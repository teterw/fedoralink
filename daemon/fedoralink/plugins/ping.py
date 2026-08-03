"""Find-my-phone, and a round-trip test that the link actually works."""

from __future__ import annotations

import logging
from typing import Any

from ..protocol import PING
from . import Plugin

log = logging.getLogger(__name__)


class PingPlugin(Plugin):
    name = "ping"
    handles = (PING,)

    def ring_phone(self) -> bool:
        """Make the phone ring at full volume even if it's silenced."""
        return self.send(PING, {"ring": True})

    def on_packet(self, packet: dict[str, Any]) -> None:
        message = packet["body"].get("message") or "Ping from your phone"
        self.daemon.notifications.show_local(
            summary=self.daemon.device_name or "FedoraLink",
            body=message,
        )
