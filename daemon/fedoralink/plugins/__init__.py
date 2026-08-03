"""Plugin base class.

A plugin declares the packet types it cares about and gets handed those
packets. Adding a feature to FedoraLink should mean writing one of these
and appending it to the list in ``daemon.py`` — nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..daemon import Daemon
    from ..transport import Connection


class Plugin:
    #: Packet types this plugin wants delivered to ``on_packet``.
    handles: tuple[str, ...] = ()

    #: Advertised to the phone in the identity handshake.
    name: str = "plugin"

    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon

    def start(self) -> None:
        """Called once at daemon startup."""

    def stop(self) -> None:
        """Called once at daemon shutdown."""

    def on_connected(self, connection: "Connection") -> None:
        """A phone just linked up."""

    def on_disconnected(self, connection: "Connection") -> None:
        """The link dropped. Tear down anything per-connection."""

    def on_packet(self, packet: dict[str, Any]) -> None:
        """A packet whose type is in ``handles`` arrived."""

    def send(self, packet_type: str, body: dict[str, Any] | None = None) -> bool:
        return self.daemon.send(packet_type, body)
