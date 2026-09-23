"""End-to-end LAN handshake, with a fake phone.

Needs PyGObject for the main loop, so it skips where that isn't present —
CI's interpreter is one such place. Locally it is the only test that drives
the real socket code rather than the pure logic underneath it.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

pytest.importorskip("gi", reason="PyGObject is needed for the GLib main loop")

from gi.repository import GLib

from fedoralink import auth
from fedoralink.session import RecordCrypto, derive_keys
from fedoralink.tcp import TcpTransport


class Harness:
    """A TcpTransport plus a main loop that stops when something happens."""

    def __init__(self, secret: str | None, device_id: str = "phone-1") -> None:
        self.secret = secret
        self.device_id = device_id
        self.connected: list = []
        self.disconnected: list = []
        self.packets: list = []

        self.transport = TcpTransport(
            on_connected=self._on_connected,
            on_disconnected=self.disconnected.append,
            on_packet=self.packets.append,
            secret_for=lambda dev: self.secret if dev == self.device_id else None,
        )
        self.loop = GLib.MainLoop()
        self.port = self.transport.start()

    def _on_connected(self, connection) -> None:
        self.connected.append(connection)
        self.loop.quit()

    def run(self, timeout_ms: int = 3000) -> None:
        """Pump the loop until something connects or the timeout fires."""
        GLib.timeout_add(timeout_ms, self.loop.quit)
        self.loop.run()

    def stop(self) -> None:
        self.transport.stop()


def hello_line(
    device_id: str, secret: str, desktop_nonce: str, phone_nonce: str
) -> bytes:
    return (
        json.dumps(
            {
                "deviceId": device_id,
                "nonce": phone_nonce,
                "mac": auth.respond(secret, desktop_nonce),
            }
        )
        + "\n"
    ).encode()


def read_line(sock: socket.socket) -> dict:
    buf = bytearray()
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError("desktop hung up without answering")
        buf.extend(chunk)
    return json.loads(bytes(buf[: buf.index(b"\n")]).decode())


class TestSuccessfulHandshake:
    def test_a_valid_phone_gets_a_session(self):
        secret = auth.new_secret()
        harness = Harness(secret)
        try:
            desktop_nonce = harness.transport.new_offer("phone-1")
            phone_nonce = auth.new_nonce()
            answer: dict = {}

            def phone():
                sock = socket.create_connection(("127.0.0.1", harness.port), 3)
                sock.sendall(hello_line("phone-1", secret, desktop_nonce, phone_nonce))
                answer.update(read_line(sock))
                # Hold the socket open; closing would race the desktop's
                # on_connected and make the assertion flaky.
                answer["_sock"] = sock

            thread = threading.Thread(target=phone, daemon=True)
            thread.start()
            harness.run()
            thread.join(3)

            assert harness.connected, "desktop never reported a LAN connection"
            # The desktop has to prove itself too.
            assert auth.verify(secret, phone_nonce, answer.get("mac"))
            answer["_sock"].close()
        finally:
            harness.stop()

    def test_packets_flow_over_the_encrypted_session(self):
        secret = auth.new_secret()
        harness = Harness(secret)
        try:
            desktop_nonce = harness.transport.new_offer("phone-1")
            phone_nonce = auth.new_nonce()
            box: dict = {}

            def phone():
                sock = socket.create_connection(("127.0.0.1", harness.port), 3)
                sock.sendall(hello_line("phone-1", secret, desktop_nonce, phone_nonce))
                read_line(sock)
                box["sock"] = sock

            thread = threading.Thread(target=phone, daemon=True)
            thread.start()
            harness.run()
            thread.join(3)
            assert harness.connected

            # Now speak the record protocol at it, as the phone would.
            desktop_key, phone_key = derive_keys(secret, desktop_nonce, phone_nonce)
            phone_crypto = RecordCrypto(send_key=phone_key, recv_key=desktop_key)
            packet = {"id": 1, "type": "fedoralink.battery", "body": {"level": 42}}
            box["sock"].sendall(phone_crypto.seal(json.dumps(packet).encode() + b"\n"))

            harness.loop = GLib.MainLoop()
            GLib.timeout_add(600, harness.loop.quit)
            harness.loop.run()

            assert harness.packets, "no packet arrived over the LAN session"
            assert harness.packets[0]["body"]["level"] == 42
            box["sock"].close()
        finally:
            harness.stop()


class TestRejections:
    def connect_and_expect_refusal(self, harness, payload: bytes) -> None:
        """Send `payload`; the desktop must not report a connection."""
        def phone():
            try:
                sock = socket.create_connection(("127.0.0.1", harness.port), 3)
                if payload:
                    sock.sendall(payload)
                sock.recv(4096)
                sock.close()
            except OSError:
                pass

        thread = threading.Thread(target=phone, daemon=True)
        thread.start()
        harness.run(timeout_ms=1200)
        thread.join(3)
        assert not harness.connected

    def test_wrong_mac_is_refused(self):
        harness = Harness(auth.new_secret())
        try:
            harness.transport.new_offer("phone-1")
            bad = json.dumps(
                {"deviceId": "phone-1", "nonce": auth.new_nonce(), "mac": "deadbeef"}
            ) + "\n"
            self.connect_and_expect_refusal(harness, bad.encode())
        finally:
            harness.stop()

    def test_unknown_device_is_refused(self):
        secret = auth.new_secret()
        harness = Harness(secret)
        try:
            nonce = harness.transport.new_offer("phone-1")
            bad = json.dumps(
                {
                    "deviceId": "someone-else",
                    "nonce": auth.new_nonce(),
                    "mac": auth.respond(secret, nonce),
                }
            ) + "\n"
            self.connect_and_expect_refusal(harness, bad.encode())
        finally:
            harness.stop()

    def test_connection_without_an_offer_is_refused(self):
        secret = auth.new_secret()
        harness = Harness(secret)
        try:
            # No new_offer() call at all.
            self.connect_and_expect_refusal(harness, b'{"deviceId":"phone-1"}\n')
        finally:
            harness.stop()

    def test_malformed_json_is_refused(self):
        harness = Harness(auth.new_secret())
        try:
            harness.transport.new_offer("phone-1")
            self.connect_and_expect_refusal(harness, b"{not json}\n")
        finally:
            harness.stop()

    def test_an_offer_is_good_for_one_attempt(self):
        secret = auth.new_secret()
        harness = Harness(secret)
        try:
            nonce = harness.transport.new_offer("phone-1")
            good = hello_line("phone-1", secret, nonce, auth.new_nonce())

            # First attempt succeeds and consumes the offer.
            def phone():
                sock = socket.create_connection(("127.0.0.1", harness.port), 3)
                sock.sendall(good)
                read_line(sock)
                sock.close()

            thread = threading.Thread(target=phone, daemon=True)
            thread.start()
            harness.run()
            thread.join(3)
            assert harness.connected

            # A replay of the same hello must not get a second session.
            harness.transport.disconnect()
            harness.connected.clear()
            harness.loop = GLib.MainLoop()
            self.connect_and_expect_refusal(harness, good)
        finally:
            harness.stop()


class TestSilentPeerDoesNotStallTheLoop:
    def test_the_main_loop_keeps_running_during_a_handshake(self):
        """The regression this suite exists for.

        The handshake used to read with a socket timeout inside the accept
        callback, so a peer that connected and said nothing froze the daemon
        for ten seconds. If that comes back, the timer below never fires.
        """
        harness = Harness(auth.new_secret())
        try:
            harness.transport.new_offer("phone-1")
            ticks = []

            def silent():
                sock = socket.create_connection(("127.0.0.1", harness.port), 3)
                # Connect, then say nothing at all.
                import time

                time.sleep(2)
                sock.close()

            thread = threading.Thread(target=silent, daemon=True)
            thread.start()

            GLib.timeout_add(150, lambda: (ticks.append(1), True)[1])
            harness.run(timeout_ms=1200)
            thread.join(4)

            # A stalled loop would produce no ticks at all.
            assert len(ticks) >= 3, f"main loop stalled: only {len(ticks)} ticks"
            assert not harness.connected
        finally:
            harness.stop()
