"""Key derivation, record framing, and AEAD behaviour."""

from __future__ import annotations

import base64
import struct

import pytest

from fedoralink.auth import new_nonce, new_secret
from fedoralink.session import (
    MAX_RECORD_BYTES,
    HandshakeRejected,
    RecordCrypto,
    RecordReader,
    SessionError,
    derive_keys,
    hkdf,
)


class TestHkdf:
    def test_rfc5869_test_case_1(self):
        # The canonical vector. Getting HKDF subtly wrong is easy and
        # silent, so this pins it against the spec rather than against
        # itself.
        ikm = bytes.fromhex("0b" * 22)
        salt = bytes.fromhex("000102030405060708090a0b0c")
        info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
        expected = (
            "3cb25f25faacd57a90434f64d0362f2a"
            "2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
            "34007208d5b887185865"
        )
        assert hkdf(ikm, salt, info, 42).hex() == expected

    def test_length_is_honoured(self):
        assert len(hkdf(b"secret", b"salt", b"info", 16)) == 16
        assert len(hkdf(b"secret", b"salt", b"info", 64)) == 64

    def test_different_info_gives_different_output(self):
        a = hkdf(b"secret", b"salt", b"one")
        b = hkdf(b"secret", b"salt", b"two")
        assert a != b


class TestDeriveKeys:
    def test_both_sides_agree(self):
        secret, dn, pn = new_secret(), new_nonce(), new_nonce()
        assert derive_keys(secret, dn, pn) == derive_keys(secret, dn, pn)

    def test_directions_use_different_keys(self):
        # One key both ways would let an attacker replay our own records
        # back at us and have them authenticate.
        send, recv = derive_keys(new_secret(), new_nonce(), new_nonce())
        assert send != recv

    def test_keys_are_256_bit(self):
        send, recv = derive_keys(new_secret(), new_nonce(), new_nonce())
        assert len(send) == 32
        assert len(recv) == 32

    def test_a_different_secret_changes_the_keys(self):
        dn, pn = new_nonce(), new_nonce()
        assert derive_keys(new_secret(), dn, pn) != derive_keys(new_secret(), dn, pn)

    def test_either_nonce_changes_the_keys(self):
        # Neither side alone decides the keys, so replaying one old nonce
        # still yields a fresh session.
        secret, dn, pn = new_secret(), new_nonce(), new_nonce()
        base = derive_keys(secret, dn, pn)
        assert derive_keys(secret, new_nonce(), pn) != base
        assert derive_keys(secret, dn, new_nonce()) != base

    def test_malformed_material_raises(self):
        with pytest.raises(SessionError, match="base64"):
            derive_keys("!!!", new_nonce(), new_nonce())

    def test_empty_material_raises(self):
        with pytest.raises(SessionError, match="empty"):
            derive_keys("", new_nonce(), new_nonce())


class TestRecordCrypto:
    def pair(self):
        """Two RecordCryptos wired as opposite ends of one session."""
        send, recv = derive_keys(new_secret(), new_nonce(), new_nonce())
        return RecordCrypto(send, recv), RecordCrypto(recv, send)

    def test_round_trip(self):
        desktop, phone = self.pair()
        sealed = desktop.seal(b'{"type":"fedoralink.ping"}')
        assert phone.open(sealed[4:]) == b'{"type":"fedoralink.ping"}'

    def test_many_records_in_order(self):
        desktop, phone = self.pair()
        for i in range(100):
            sealed = desktop.seal(f"packet {i}".encode())
            assert phone.open(sealed[4:]) == f"packet {i}".encode()

    def test_ciphertext_differs_for_identical_plaintext(self):
        # The counter advances, so the same message never seals the same
        # way twice. Identical ciphertext would leak repetition.
        desktop, _ = self.pair()
        first = desktop.seal(b"same")
        second = desktop.seal(b"same")
        assert first != second

    def test_length_prefix_matches_the_payload(self):
        desktop, _ = self.pair()
        sealed = desktop.seal(b"hello")
        (length,) = struct.unpack("!I", sealed[:4])
        assert length == len(sealed) - 4

    def test_tampered_record_is_rejected(self):
        desktop, phone = self.pair()
        sealed = bytearray(desktop.seal(b"important")[4:])
        sealed[-1] ^= 0x01
        with pytest.raises(SessionError, match="authentication"):
            phone.open(bytes(sealed))

    def test_wrong_key_is_rejected(self):
        desktop, _ = self.pair()
        _, stranger = self.pair()
        with pytest.raises(SessionError):
            stranger.open(desktop.seal(b"secret")[4:])

    def test_replayed_record_is_rejected(self):
        # The receive counter has moved on, so the nonce no longer matches.
        desktop, phone = self.pair()
        sealed = desktop.seal(b"first")[4:]
        phone.open(sealed)
        with pytest.raises(SessionError):
            phone.open(sealed)

    def test_out_of_order_record_is_rejected(self):
        desktop, phone = self.pair()
        first = desktop.seal(b"first")[4:]
        second = desktop.seal(b"second")[4:]
        with pytest.raises(SessionError):
            phone.open(second)
        # And the counter didn't advance, so the correct one still works.
        assert phone.open(first) == b"first"

    def test_empty_plaintext_round_trips(self):
        desktop, phone = self.pair()
        assert phone.open(desktop.seal(b"")[4:]) == b""


class TestRecordReader:
    def sealed(self, payload: bytes) -> bytes:
        return struct.pack("!I", len(payload)) + payload

    def test_one_record(self):
        reader = RecordReader()
        assert list(reader.feed(self.sealed(b"abc"))) == [b"abc"]

    def test_several_records_in_one_feed(self):
        reader = RecordReader()
        data = self.sealed(b"one") + self.sealed(b"two")
        assert list(reader.feed(data)) == [b"one", b"two"]

    def test_record_split_across_reads(self):
        reader = RecordReader()
        data = self.sealed(b"hello world")
        assert list(reader.feed(data[:6])) == []
        assert list(reader.feed(data[6:])) == [b"hello world"]

    def test_length_prefix_split_across_reads(self):
        reader = RecordReader()
        data = self.sealed(b"xy")
        assert list(reader.feed(data[:2])) == []
        assert list(reader.feed(data[2:])) == [b"xy"]

    def test_byte_by_byte(self):
        reader = RecordReader()
        data = self.sealed(b"drip")
        out = []
        for i in range(len(data)):
            out.extend(reader.feed(data[i : i + 1]))
        assert out == [b"drip"]

    def test_ciphertext_containing_newlines_is_not_split(self):
        # The whole reason this framing isn't newline-delimited.
        reader = RecordReader()
        payload = b"aa\nbb\ncc"
        assert list(reader.feed(self.sealed(payload))) == [payload]

    def test_oversize_claim_raises_and_clears(self):
        reader = RecordReader()
        with pytest.raises(SessionError, match="claims"):
            list(reader.feed(struct.pack("!I", MAX_RECORD_BYTES + 1)))
        # Buffer cleared, so a good record afterwards still parses.
        assert list(reader.feed(self.sealed(b"ok"))) == [b"ok"]

    def test_zero_length_claim_raises(self):
        reader = RecordReader()
        with pytest.raises(SessionError, match="zero"):
            list(reader.feed(struct.pack("!I", 0)))

    def test_reset_discards_a_partial_record(self):
        reader = RecordReader()
        list(reader.feed(self.sealed(b"partial")[:5]))
        reader.reset()
        assert list(reader.feed(self.sealed(b"fresh"))) == [b"fresh"]


class TestEndToEnd:
    def test_reader_and_crypto_together(self):
        secret, dn, pn = new_secret(), new_nonce(), new_nonce()
        send, recv = derive_keys(secret, dn, pn)
        desktop = RecordCrypto(send, recv)
        phone = RecordCrypto(recv, send)

        # Three packets concatenated, then delivered in awkward chunks.
        wire = b"".join(desktop.seal(f'{{"n":{i}}}'.encode()) for i in range(3))
        reader = RecordReader()

        opened = []
        for i in range(0, len(wire), 7):
            for record in reader.feed(wire[i : i + 7]):
                opened.append(phone.open(record))

        assert opened == [b'{"n":0}', b'{"n":1}', b'{"n":2}']

    def test_session_material_is_base64_transportable(self):
        # It travels inside a JSON packet, so it has to survive that.
        secret = new_secret()
        assert base64.b64decode(secret, validate=True)


class TestVerifyHello:
    """Every way a LAN peer can be refused.

    This is the function that decides whether a stranger gets onto the
    link, so each rejection path is pinned rather than trusted.
    """

    def setup_method(self):
        from fedoralink import auth

        self.auth = auth
        self.secret = auth.new_secret()
        self.desktop_nonce = auth.new_nonce()
        self.phone_nonce = auth.new_nonce()
        self.device = "phone-1"

    def hello(self, **overrides):
        base = {
            "deviceId": self.device,
            "nonce": self.phone_nonce,
            "mac": self.auth.respond(self.secret, self.desktop_nonce),
        }
        base.update(overrides)
        return base

    def verify(self, hello=None, **kwargs):
        from fedoralink.session import verify_hello

        args = {
            "expected_device": self.device,
            "desktop_nonce": self.desktop_nonce,
            "secret": self.secret,
        }
        args.update(kwargs)
        return verify_hello(hello if hello is not None else self.hello(), **args)

    def test_a_good_hello_returns_the_phone_nonce(self):
        assert self.verify() == self.phone_nonce

    def test_no_outstanding_offer_is_refused(self):
        # An unsolicited connection has no nonce to have answered.
        with pytest.raises(HandshakeRejected, match="no upgrade offer"):
            self.verify(desktop_nonce=None)

    def test_offer_to_another_device_is_refused(self):
        with pytest.raises(HandshakeRejected, match="not the device"):
            self.verify(expected_device="someone-else")

    def test_wrong_device_id_is_refused(self):
        with pytest.raises(HandshakeRejected, match="not the device"):
            self.verify(self.hello(deviceId="impostor"))

    def test_missing_device_id_is_refused(self):
        with pytest.raises(HandshakeRejected, match="not the device"):
            self.verify({"nonce": self.phone_nonce, "mac": "x"})

    def test_missing_nonce_is_refused(self):
        with pytest.raises(HandshakeRejected, match="no nonce"):
            self.verify(self.hello(nonce=None))

    def test_empty_nonce_is_refused(self):
        with pytest.raises(HandshakeRejected, match="no nonce"):
            self.verify(self.hello(nonce=""))

    def test_non_string_nonce_is_refused(self):
        with pytest.raises(HandshakeRejected, match="no nonce"):
            self.verify(self.hello(nonce=1234))

    def test_unknown_device_with_no_secret_is_refused(self):
        with pytest.raises(HandshakeRejected, match="no secret"):
            self.verify(secret=None)

    def test_wrong_mac_is_refused(self):
        with pytest.raises(HandshakeRejected, match="did not verify"):
            self.verify(self.hello(mac="deadbeef"))

    def test_missing_mac_is_refused(self):
        with pytest.raises(HandshakeRejected, match="did not verify"):
            self.verify(self.hello(mac=None))

    def test_mac_for_a_different_nonce_is_refused(self):
        # A recorded handshake replayed into a new session: the offer nonce
        # is fresh each time, so the old MAC no longer answers it.
        stale = self.auth.respond(self.secret, self.auth.new_nonce())
        with pytest.raises(HandshakeRejected, match="did not verify"):
            self.verify(self.hello(mac=stale))

    def test_mac_from_a_different_secret_is_refused(self):
        other = self.auth.respond(self.auth.new_secret(), self.desktop_nonce)
        with pytest.raises(HandshakeRejected, match="did not verify"):
            self.verify(self.hello(mac=other))

    def test_non_string_mac_is_refused_not_raised(self):
        with pytest.raises(HandshakeRejected, match="did not verify"):
            self.verify(self.hello(mac={"nope": 1}))
