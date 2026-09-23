"""The LAN transport: same packets, faster pipe, encrypted by us.

**Discovery is the Bluetooth link.** The roadmap left this open between
mDNS and reusing Bluetooth; Bluetooth wins on both counts. It needs no new
dependency and no multicast working through the local firewall, and the
address arrives over a channel that is already authenticated — so there is
no discovery to spoof. The cost is that a LAN session only starts after a
Bluetooth handshake, which is the right order anyway: that is where the
device secret and the session nonces come from.

Sequence, once Bluetooth has authenticated:

1. Desktop listens, then offers ``fedoralink.upgrade`` over Bluetooth with
   its address, port and a fresh nonce.
2. Phone opens TCP and sends one plaintext line: its device id, its own
   nonce, and ``HMAC(secret, desktop_nonce)``.
3. Desktop checks that MAC, answers with ``HMAC(secret, phone_nonce)`` so
   the phone can check *it*, and both derive keys from
   ``HKDF(secret, desktop_nonce || phone_nonce)``.
4. Everything after is AES-256-GCM records.

The handshake is plaintext, and that is fine: the nonces are public and
the MACs only prove knowledge of a secret that never crosses the wire. The
desktop nonce is fresh per offer, so a recorded handshake cannot be
replayed into a new session.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from typing import Any

from gi.repository import GLib

from . import auth
from .protocol import PacketReader, ProtocolError, serialize
from .session import (
    HandshakeRejected,
    RecordCrypto,
    RecordReader,
    SessionError,
    derive_keys,
    verify_hello,
)

log = logging.getLogger(__name__)

# 0 lets the kernel pick, which avoids fighting over a fixed port and
# means the phone is told where to go anyway.
DEFAULT_PORT = 0

# A phone that opens the socket and then says nothing must not hold the
# single accept slot forever.
HANDSHAKE_TIMEOUT_SECONDS = 10


def local_addresses() -> list[str]:
    """This machine's LAN addresses, best first.

    The phone cannot work out where the PC is from a Bluetooth link, so the
    upgrade offer has to name it. The UDP trick finds the address on the
    default route without sending anything — connecting a datagram socket
    only sets its destination — and getaddrinfo fills in any others, for a
    host with several interfaces.
    """
    found: list[str] = []

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Any routable address will do; nothing is transmitted.
        probe.connect(("192.0.2.1", 9))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    addresses: list[str] = []
    for address in found:
        if address in addresses:
            continue
        # Loopback is useless to the phone, and link-local means no DHCP,
        # so the two are very unlikely to reach each other.
        if address.startswith(("127.", "169.254.")):
            continue
        addresses.append(address)

    return addresses


class TcpConnection:
    """One authenticated, encrypted LAN link."""

    def __init__(
        self,
        sock: socket.socket,
        peer_name: str,
        crypto: RecordCrypto,
        on_packet: Callable[[dict[str, Any]], None],
        on_close: Callable[[TcpConnection], None],
    ) -> None:
        self.sock = sock
        self.peer_name = peer_name
        # No BlueZ object for a TCP peer; the audio plugin checks for this.
        self.device_path: str | None = None
        self.device_name = peer_name

        self._crypto = crypto
        self._on_packet = on_packet
        self._on_close = on_close
        self._records = RecordReader()
        self._packets = PacketReader()
        self._outbox = bytearray()
        self._closed = False

        self.sock.setblocking(False)
        self._in_source = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            self.sock.fileno(),
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._on_readable,
        )
        self._out_source: int | None = None

    @property
    def pending_bytes(self) -> int:
        """Bytes queued but not yet written to the socket."""
        return len(self._outbox)

    # ----------------------------------------------------------- inbound

    def _on_readable(self, _fd: int, condition: GLib.IOCondition) -> bool:
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            log.info("LAN link to %s hung up", self.peer_name)
            self.close()
            return GLib.SOURCE_REMOVE

        try:
            chunk = self.sock.recv(65536)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        except OSError as exc:
            log.info("LAN read from %s failed: %s", self.peer_name, exc)
            self.close()
            return GLib.SOURCE_REMOVE

        if not chunk:
            self.close()
            return GLib.SOURCE_REMOVE

        try:
            for record in self._records.feed(chunk):
                self._handle_record(record)
        except SessionError as exc:
            # Framing or authentication failure. Unlike a malformed JSON
            # packet this is not recoverable: the record counters are now
            # out of step, so the only safe move is to hang up.
            log.error("LAN session with %s failed: %s", self.peer_name, exc)
            self.close()
            return GLib.SOURCE_REMOVE

        return GLib.SOURCE_CONTINUE

    def _handle_record(self, record: bytes) -> None:
        plaintext = self._crypto.open(record)
        try:
            for packet in self._packets.feed(plaintext):
                self._on_packet(packet)
        except ProtocolError as exc:
            # The record authenticated, so the peer is genuine and just
            # sent something malformed. Skip it and keep the link.
            log.warning("malformed packet inside a good record: %s", exc)

    # ---------------------------------------------------------- outbound

    def send(self, packet: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._outbox.extend(self._crypto.seal(serialize(packet)))
        except SessionError as exc:
            log.error("could not seal a packet: %s", exc)
            self.close()
            return
        self._flush()

    def _flush(self) -> None:
        while self._outbox:
            try:
                sent = self.sock.send(self._outbox)
            except BlockingIOError:
                self._watch_writable()
                return
            except OSError as exc:
                log.info("LAN write to %s failed: %s", self.peer_name, exc)
                self.close()
                return
            del self._outbox[:sent]

        self._unwatch_writable()

    def _watch_writable(self) -> None:
        if self._out_source is not None:
            return
        self._out_source = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            self.sock.fileno(),
            GLib.IOCondition.OUT,
            self._on_writable,
        )

    def _unwatch_writable(self) -> None:
        if self._out_source is not None:
            GLib.source_remove(self._out_source)
            self._out_source = None

    def _on_writable(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        self._flush()
        return GLib.SOURCE_CONTINUE if self._outbox else GLib.SOURCE_REMOVE

    # ------------------------------------------------------------- close

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        self._unwatch_writable()
        if self._in_source is not None:
            GLib.source_remove(self._in_source)
            self._in_source = None

        try:
            self.sock.close()
        except OSError:
            pass

        self._on_close(self)


class TcpTransport:
    """Listens for a phone that has been handed an upgrade offer."""

    def __init__(
        self,
        on_connected: Callable[[TcpConnection], None],
        on_disconnected: Callable[[TcpConnection], None],
        on_packet: Callable[[dict[str, Any]], None],
        secret_for: Callable[[str], str | None],
    ) -> None:
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_packet = on_packet
        self._secret_for = secret_for

        self.connection: TcpConnection | None = None
        self._server: socket.socket | None = None
        self._accept_source: int | None = None
        # Nonce from the offer currently outstanding, and who it was for.
        self._nonce: str | None = None
        self._expect_device: str | None = None

    # ------------------------------------------------------------ listen

    def start(self, port: int = DEFAULT_PORT) -> int | None:
        """Bind and listen. Returns the port, or None if it failed."""
        if self._server is not None:
            return self._server.getsockname()[1]

        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", port))
            server.listen(1)
            server.setblocking(False)
        except OSError as exc:
            log.warning("could not listen for LAN links: %s", exc)
            return None

        self._server = server
        self._accept_source = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            server.fileno(),
            GLib.IOCondition.IN,
            self._on_incoming,
        )

        bound = server.getsockname()[1]
        log.info("listening for LAN links on port %d", bound)
        return bound

    def stop(self) -> None:
        if self.connection is not None:
            self.connection.close()
        if self._accept_source is not None:
            GLib.source_remove(self._accept_source)
            self._accept_source = None
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        self._nonce = None
        self._expect_device = None

    # ------------------------------------------------------------- offer

    def new_offer(self, device_id: str) -> str:
        """Mint the nonce for an upgrade offer to `device_id`."""
        self._nonce = auth.new_nonce()
        self._expect_device = device_id
        return self._nonce

    # ------------------------------------------------------------ accept

    def _on_incoming(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        if self._server is None:
            return GLib.SOURCE_REMOVE

        try:
            sock, address = self._server.accept()
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        except OSError as exc:
            log.warning("LAN accept failed: %s", exc)
            return GLib.SOURCE_CONTINUE

        if self.connection is not None:
            # One link at a time; a second caller is not our phone.
            log.info("refusing a second LAN link from %s", address[0])
            sock.close()
            return GLib.SOURCE_CONTINUE

        # Blocking for the handshake only: it is a single short line, and
        # the timeout stops a silent peer holding the slot.
        sock.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
        self._handshake(sock, address[0])
        return GLib.SOURCE_CONTINUE

    def _handshake(self, sock: socket.socket, host: str) -> None:
        nonce, expected_device = self._nonce, self._expect_device
        # One offer, one attempt.
        self._nonce = None
        self._expect_device = None

        if nonce is None or expected_device is None:
            log.warning("unsolicited LAN link from %s", host)
            sock.close()
            return

        try:
            hello = self._read_line(sock)
        except (OSError, ValueError) as exc:
            log.warning("LAN handshake with %s failed: %s", host, exc)
            sock.close()
            return

        device_id = hello.get("deviceId")
        secret = self._secret_for(device_id) if isinstance(device_id, str) else None

        try:
            phone_nonce = verify_hello(hello, expected_device, nonce, secret)
        except HandshakeRejected as exc:
            log.warning("refusing LAN peer %s: %s", host, exc)
            sock.close()
            return

        assert secret is not None  # verify_hello rejects None

        # Prove ourselves in return, so the phone isn't trusting an
        # address it was handed.
        try:
            our_mac = auth.respond(secret, phone_nonce)
            sock.sendall(serialize({"mac": our_mac}))
        except (OSError, ValueError) as exc:
            log.warning("could not answer the LAN challenge: %s", exc)
            sock.close()
            return

        try:
            desktop_key, phone_key = derive_keys(secret, nonce, phone_nonce)
            crypto = RecordCrypto(send_key=desktop_key, recv_key=phone_key)
        except SessionError as exc:
            log.error("could not derive LAN session keys: %s", exc)
            sock.close()
            return

        sock.settimeout(None)
        connection = TcpConnection(
            sock, host, crypto, self._on_packet, self._handle_close
        )
        self.connection = connection
        log.info("LAN link up with %s", host)
        self._on_connected(connection)

    @staticmethod
    def _read_line(sock: socket.socket) -> dict[str, Any]:
        """Read one newline-terminated JSON object, with a hard cap."""
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ValueError("peer hung up during the handshake")
            buf.extend(chunk)
            if len(buf) > 8192:
                raise ValueError("handshake line too long")

        parsed = json.loads(bytes(buf[: buf.index(b"\n")]).decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("handshake is not an object")
        return parsed

    # -------------------------------------------------------------- send

    def send(self, packet: dict[str, Any]) -> bool:
        if self.connection is None:
            return False
        self.connection.send(packet)
        return True

    def disconnect(self) -> None:
        if self.connection is not None:
            self.connection.close()

    def _handle_close(self, connection: TcpConnection) -> None:
        if self.connection is connection:
            self.connection = None
        self._on_disconnected(connection)
