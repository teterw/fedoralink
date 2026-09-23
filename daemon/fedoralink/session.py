"""Key derivation and record encryption for the LAN transport.

Bluetooth gives us an encrypted link for free. TCP does not, and a LAN is
a far more hostile place than an RFCOMM pairing — anything on the same
Wi-Fi would otherwise read the clipboard and every mirrored notification.

So the TCP transport reuses the device secret the Bluetooth handshake
already established. Both sides contribute a random nonce, HKDF turns
secret + nonces into two directional keys, and every packet travels as an
AES-256-GCM record. Directional keys matter: one key in both directions
would let an attacker replay our own records back at us.

Framing is length-prefixed rather than newline-delimited, because
ciphertext contains arbitrary bytes and a newline inside one would split a
record in half.

Requires the `cryptography` package for AES-GCM, which the stdlib has no
equivalent of. Everything else here is stdlib, and the derivation is
unit-tested without it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import struct
from typing import Any

from . import auth

log = logging.getLogger(__name__)

KEY_BYTES = 32
NONCE_BYTES = 12
LENGTH_PREFIX = 4

# One record cannot exceed this. Same reasoning as MAX_LINE_BYTES in
# protocol.py: a desynced or hostile peer must not be able to make us
# allocate without bound, and the length prefix is attacker-controlled.
MAX_RECORD_BYTES = 8 * 1024 * 1024

HKDF_INFO_DESKTOP = b"fedoralink desktop->phone"
HKDF_INFO_PHONE = b"fedoralink phone->desktop"


class SessionError(Exception):
    """A record could not be sealed or opened. The session is not usable."""


class HandshakeRejected(Exception):
    """The peer may not have a LAN session. Carries the reason for the log."""


def verify_hello(
    hello: dict[str, Any],
    expected_device: str | None,
    desktop_nonce: str | None,
    secret: str | None,
) -> str:
    """Decide whether a LAN hello may proceed. Returns the phone's nonce.

    Separated from the socket work deliberately: this is the part where a
    mistake lets a stranger onto the link, so it is a pure function with no
    I/O and every rejection path under test.
    """
    if desktop_nonce is None or expected_device is None:
        raise HandshakeRejected("no upgrade offer is outstanding")

    if hello.get("deviceId") != expected_device:
        raise HandshakeRejected("peer is not the device we offered to")

    phone_nonce = hello.get("nonce")
    if not isinstance(phone_nonce, str) or not phone_nonce:
        raise HandshakeRejected("peer sent no nonce")

    if secret is None:
        raise HandshakeRejected("no secret stored for that device")

    if not auth.verify(secret, desktop_nonce, hello.get("mac")):
        raise HandshakeRejected("challenge response did not verify")

    return phone_nonce


def hkdf(secret: bytes, salt: bytes, info: bytes, length: int = KEY_BYTES) -> bytes:
    """HKDF-SHA256, per RFC 5869.

    Written out rather than imported so key derivation stays testable
    without the cryptography package, and so both sides can be checked
    against the same vectors.
    """
    prk = hmac.new(salt, secret, hashlib.sha256).digest()

    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(
            prk, block + info + bytes([counter]), hashlib.sha256
        ).digest()
        out += block
        counter += 1
    return out[:length]


def derive_keys(
    secret_b64: str, desktop_nonce_b64: str, phone_nonce_b64: str
) -> tuple[bytes, bytes]:
    """Return (desktop_to_phone_key, phone_to_desktop_key).

    Both nonces go into the salt, so neither side alone decides the keys —
    a peer that replayed an old nonce would still get fresh keys, and a
    recorded session can't be decrypted against a later one.
    """
    try:
        secret = base64.b64decode(secret_b64, validate=True)
        desktop_nonce = base64.b64decode(desktop_nonce_b64, validate=True)
        phone_nonce = base64.b64decode(phone_nonce_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise SessionError("session material is not valid base64") from exc

    if not secret or not desktop_nonce or not phone_nonce:
        raise SessionError("session material is empty")

    salt = desktop_nonce + phone_nonce
    return (
        hkdf(secret, salt, HKDF_INFO_DESKTOP),
        hkdf(secret, salt, HKDF_INFO_PHONE),
    )


class RecordCrypto:
    """Seals and opens AES-256-GCM records with a per-direction counter.

    The GCM nonce is the record counter, never random: GCM fails
    catastrophically on nonce reuse under the same key, and a counter
    cannot collide the way 96 random bits eventually can. Each direction
    has its own key and its own counter, so the two never overlap.
    """

    def __init__(self, send_key: bytes, recv_key: bytes) -> None:
        # Imported here so this module can be imported — and its
        # derivation tested — on a system without the package.
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise SessionError(
                "the cryptography package is required for the LAN transport; "
                "install python3-cryptography"
            ) from exc

        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_counter = 0
        self._recv_counter = 0

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return counter.to_bytes(NONCE_BYTES, "big")

    def seal(self, plaintext: bytes) -> bytes:
        """Encrypt one record, length-prefixed and ready for the wire."""
        nonce = self._nonce(self._send_counter)
        self._send_counter += 1
        sealed = self._send.encrypt(nonce, plaintext, None)
        return struct.pack("!I", len(sealed)) + sealed

    def open(self, sealed: bytes) -> bytes:
        """Decrypt one record.

        The counter advances only on success, so a corrupted record cannot
        desynchronise the stream — though it does mean a caller must treat
        a failure as fatal to the session rather than skipping ahead.
        """
        nonce = self._nonce(self._recv_counter)
        try:
            plaintext = self._recv.decrypt(nonce, sealed, None)
        except Exception as exc:
            # Wrong key, tampered record, or a replayed one at the wrong
            # position. None of them are recoverable.
            raise SessionError("record failed authentication") from exc
        self._recv_counter += 1
        return plaintext


class RecordReader:
    """Reassembles length-prefixed records from a byte stream.

    Same job as protocol.PacketReader, different framing: ciphertext can
    contain any byte, so a newline delimiter would cut records in half.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes):
        """Append bytes; yield each complete sealed record."""
        self._buf.extend(chunk)

        while True:
            if len(self._buf) < LENGTH_PREFIX:
                return

            (length,) = struct.unpack("!I", bytes(self._buf[:LENGTH_PREFIX]))
            if length > MAX_RECORD_BYTES:
                self._buf.clear()
                raise SessionError(f"record claims {length} bytes")
            if length == 0:
                raise SessionError("record claims zero bytes")

            end = LENGTH_PREFIX + length
            if len(self._buf) < end:
                return

            record = bytes(self._buf[LENGTH_PREFIX:end])
            del self._buf[:end]
            yield record

    def reset(self) -> None:
        self._buf.clear()
