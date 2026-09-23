"""Find-my-phone, find-my-PC, and a round-trip test that the link works."""

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
        body = packet["body"]

        # Three cases, and conflating them would show a spurious
        # notification when the phone asks us to stop.
        if "ring" not in body:
            # A plain ping — the round-trip link test. Say so quietly.
            message = body.get("message") or "Ping from your phone"
            self.daemon.notifications.show_local(
                summary=self.daemon.device_name or "FedoraLink",
                body=message,
            )
            return

        if not body["ring"]:
            self.stop_pc_ring()
            return

        # Find-my-PC. A silent notification is no use for locating a laptop
        # under a cushion, so make actual noise.
        seconds = self.daemon.config["pc_ring_seconds"]
        self.daemon.alerter.start(seconds)

        self.daemon.notifications.show_local(
            summary=f"{self.daemon.device_name or 'Your phone'} is ringing this PC",
            body=body.get("message") or "Sent from FedoraLink on your phone.",
            urgent=True,
            # The alerter decides: None when it can ring properly itself,
            # a sound-theme name when the notification is the only noise.
            sound_name=self.daemon.alerter.fallback_sound_name,
        )

    def stop_pc_ring(self) -> None:
        """Silence a find-my-PC alert from the desktop side."""
        self.daemon.alerter.stop()
