"""Phone battery level, surfaced in the Quick Settings toggle."""

from __future__ import annotations

import logging
from typing import Any

from . import Plugin
from ..protocol import BATTERY

log = logging.getLogger(__name__)


class BatteryPlugin(Plugin):
    name = "battery"
    handles = (BATTERY,)

    def on_disconnected(self, connection) -> None:
        # Stale battery readings are worse than none — the shell should
        # show "disconnected", not the last level from an hour ago.
        self.daemon.set_battery(None, False)

    def on_packet(self, packet: dict[str, Any]) -> None:
        body = packet["body"]
        level = body.get("level")
        charging = bool(body.get("charging", False))

        if not isinstance(level, int) or not 0 <= level <= 100:
            log.warning("ignoring out-of-range battery level: %r", level)
            return

        self.daemon.set_battery(level, charging)
