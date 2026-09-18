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

    def stop_ringing(self) -> bool:
        """Silence a ring already in progress, before its own timeout.

        Same packet type as ``ring_phone`` rather than a new one: a phone
        built before this existed only acts on ``ring: true``, so it
        ignores this and stays connected instead of choking on an
        unknown type.
        """
        return self.send(PING, {"ring": False})

    def on_packet(self, packet: dict[str, Any]) -> None:
        message = packet["body"].get("message") or "Ping from your phone"
        self.daemon.notifications.show_local(
            summary=self.daemon.device_name or "FedoraLink",
            body=message,
        )
