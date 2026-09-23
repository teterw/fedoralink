"""What the phone is playing, and the buttons to control it.

Bluetooth's own AVRCP does this — but only while A2DP is connected, and
``connect_phone_audio`` defaults to off so the phone's music doesn't come
out of the laptop. Dropping A2DP takes AVRCP down with it, so without this
the media keys go too. That's what makes carrying it over RFCOMM worth the
packets rather than a duplicate of a working profile.

No gi here: the state is plain data, mirrored onto D-Bus by the daemon.
"""

from __future__ import annotations

import logging
from typing import Any

from ..protocol import MEDIA
from . import Plugin

log = logging.getLogger(__name__)

# Commands the phone understands. Anything else is refused here rather
# than sent and silently ignored at the far end.
ACTIONS = frozenset({"play", "pause", "playpause", "next", "previous"})


class MediaPlugin(Plugin):
    name = "media"
    handles = (MEDIA,)

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        self.has_session = False
        self.playing = False
        self.title = ""
        self.artist = ""

    def on_disconnected(self, connection) -> None:
        # A stale track with no phone attached is worse than an empty row.
        self._clear()

    def on_packet(self, packet: dict[str, Any]) -> None:
        body = packet["body"]

        if not body.get("hasSession"):
            self._clear()
            return

        title = body.get("title") or ""
        artist = body.get("artist") or ""
        playing = bool(body.get("playing", False))

        if not isinstance(title, str) or not isinstance(artist, str):
            log.warning("ignoring media packet with non-string metadata")
            return

        changed = (
            not self.has_session
            or playing != self.playing
            or title != self.title
            or artist != self.artist
        )

        self.has_session = True
        self.playing = playing
        self.title = title
        self.artist = artist

        if changed:
            self.daemon.dbus.emit_changed()

    def command(self, action: str) -> bool:
        if action not in ACTIONS:
            log.warning("refusing unknown media action %r", action)
            return False
        return self.send(MEDIA, {"action": action})

    def _clear(self) -> None:
        if not (self.has_session or self.title or self.artist):
            return
        self.has_session = False
        self.playing = False
        self.title = ""
        self.artist = ""
        self.daemon.dbus.emit_changed()
