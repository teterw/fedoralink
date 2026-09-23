"""Phone battery level, surfaced in the Quick Settings toggle."""

from __future__ import annotations

import logging
from typing import Any

from ..protocol import BATTERY
from . import Plugin

log = logging.getLogger(__name__)


class BatteryPlugin(Plugin):
    name = "battery"
    handles = (BATTERY,)

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        # Set while a low-battery warning is standing, so a phone sitting
        # at 12% doesn't post a notification every time it reports in.
        self._warned = False

    def on_disconnected(self, connection) -> None:
        # Stale battery readings are worse than none — the shell should
        # show "disconnected", not the last level from an hour ago.
        self.daemon.set_battery(None, False)
        # Re-arm: the next connection should warn again if it's still low.
        self._warned = False

    def on_packet(self, packet: dict[str, Any]) -> None:
        body = packet["body"]
        level = body.get("level")
        charging = bool(body.get("charging", False))

        if not isinstance(level, int) or not 0 <= level <= 100:
            log.warning("ignoring out-of-range battery level: %r", level)
            return

        self.daemon.set_battery(level, charging)
        self._check_low(level, charging)

    def _check_low(self, level: int, charging: bool) -> None:
        config = self.daemon.config
        if not config["battery_low_warning"]:
            return

        threshold = config["battery_low_threshold"]

        # Plugged in counts as recovered even below the threshold: the
        # number is on its way up and a warning would be noise.
        if charging or level > threshold:
            self._warned = False
            return

        if self._warned:
            return

        self._warned = True
        self.daemon.notifications.show_local(
            summary=f"{self.daemon.device_name or 'Phone'} battery low",
            body=f"{level}% remaining.",
        )
