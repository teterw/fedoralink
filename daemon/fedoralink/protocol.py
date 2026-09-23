"""Wire protocol: newline-delimited JSON over an RFCOMM stream.

Every packet is a single UTF-8 JSON object on one line:

    {"id": 1712345678901, "type": "fedoralink.battery", "body": {...}}

Keeping it line-oriented means the whole link can be debugged with a
terminal, and adding a feature is just adding a new ``type``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

# Bumped when a change would break an older peer. The identity exchange
# carries it so each side can refuse or degrade rather than misbehave.
#
# 2 added authentication. There is no degrading past this one: a version 1
# peer has no device secret, and the whole point is that an unauthenticated
# peer gets nothing.
PROTOCOL_VERSION = 2
MIN_PROTOCOL_VERSION = 2

# RFCOMM service UUID. Must match SERVICE_UUID in the Android app.
SERVICE_UUID = "3a94ef31-dc98-495b-bf8b-e4796714e90c"

# Packet types
IDENTITY = "fedoralink.identity"
BATTERY = "fedoralink.battery"
NOTIFICATION = "fedoralink.notification"
NOTIFICATION_DISMISS = "fedoralink.notification.dismiss"
NOTIFICATION_ACTION = "fedoralink.notification.action"
CLIPBOARD = "fedoralink.clipboard"
PING = "fedoralink.ping"
AUTH = "fedoralink.auth"
MEDIA = "fedoralink.media"
INPUT = "fedoralink.input"
UPGRADE = "fedoralink.upgrade"

# File transfer. Chunks carry base64 inside the same NDJSON stream rather
# than switching to a binary framing: it costs a third more bytes, which is
# irrelevant on a LAN and painful over Bluetooth, but it keeps the whole
# protocol readable in a terminal — the property that makes it debuggable.
FILE_OFFER = "fedoralink.file.offer"
FILE_ACCEPT = "fedoralink.file.accept"
FILE_CHUNK = "fedoralink.file.chunk"
FILE_DONE = "fedoralink.file.done"
FILE_CANCEL = "fedoralink.file.cancel"

# A phone can legitimately push a large clipboard, but nothing in this
# protocol has any business being megabytes. Cap it so a desync or a
# hostile peer can't make the daemon allocate without bound.
MAX_LINE_BYTES = 512 * 1024


def make_packet(packet_type: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": int(time.time() * 1000),
        "type": packet_type,
        "body": body or {},
    }


def serialize(packet: dict[str, Any]) -> bytes:
    """Encode one packet as a single line. Rejects embedded newlines."""
    # separators without spaces keeps packets small; ensure_ascii=False so
    # non-Latin notification text doesn't balloon into \uXXXX escapes.
    line = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    return line.encode("utf-8") + b"\n"


class PacketReader:
    """Reassembles packets from arbitrarily-chunked socket reads.

    RFCOMM delivers a byte stream, so a single read can contain half a
    packet, three packets, or a packet split mid-multibyte-character.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> Iterator[dict[str, Any]]:
        """Append raw bytes; yield each complete packet parsed."""
        self._buf.extend(chunk)

        while True:
            newline = self._buf.find(b"\n")
            if newline == -1:
                if len(self._buf) > MAX_LINE_BYTES:
                    self._buf.clear()
                    raise ProtocolError(
                        f"packet exceeded {MAX_LINE_BYTES} bytes with no newline"
                    )
                return

            line = bytes(self._buf[:newline])
            del self._buf[: newline + 1]

            if not line.strip():
                continue

            try:
                packet = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                # One bad packet shouldn't kill the link — the framing is
                # still intact, so skip it and keep reading.
                raise ProtocolError(f"malformed packet: {exc}") from exc

            if not isinstance(packet, dict) or "type" not in packet:
                raise ProtocolError("packet missing 'type'")

            packet.setdefault("body", {})
            yield packet

    def reset(self) -> None:
        self._buf.clear()


class ProtocolError(Exception):
    """A packet was malformed. Recoverable — the stream is still framed."""
