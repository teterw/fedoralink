"""Daemon core: owns the transport, the plugins, and the shared state."""

from __future__ import annotations

import hashlib
import logging
import signal
from pathlib import Path
from typing import Any

from gi.repository import Gio, GLib

from . import config as config_module
from .alert import Alerter
from .dbus_service import DBusService
from .plugins.audio import AudioPlugin
from .plugins.auth import AuthPlugin
from .plugins.battery import BatteryPlugin
from .plugins.clipboard import ClipboardPlugin
from .plugins.files import FilesPlugin
from .plugins.media import MediaPlugin
from .plugins.notification import NotificationPlugin
from .plugins.ping import PingPlugin
from .plugins.presence import PresencePlugin
from .protocol import (
    AUTH,
    IDENTITY,
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    SERVICE_UUID,
    UPGRADE,
    make_packet,
)
from .tcp import TcpTransport, local_addresses
from .transport import Connection, RfcommTransport

log = logging.getLogger(__name__)

# How often the PC tries to reach a bonded phone that isn't connected.
# The phone reconnects far more aggressively; this is just a safety net
# for when its service was killed and restarted out of range.
RECONNECT_INTERVAL_SECONDS = 60


class Daemon:
    def __init__(self) -> None:
        self.config = config_module.load()
        self.alerter = Alerter()

        self.transport = RfcommTransport(
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_packet=self._on_packet,
        )
        # The LAN link rides on trust the Bluetooth session established,
        # so it gets its own callbacks: coming up must not re-run the
        # identity handshake, and going away must not look like the phone
        # left — Bluetooth is still there underneath.
        self.tcp = TcpTransport(
            on_connected=self._on_tcp_connected,
            on_disconnected=self._on_tcp_disconnected,
            on_packet=self._on_packet,
            secret_for=lambda device_id: self.auth.store.secret_for(device_id),
        )

        self.notifications = NotificationPlugin(self)
        self.clipboard = ClipboardPlugin(self)
        self.ping = PingPlugin(self)
        self.auth = AuthPlugin(self)
        self.media = MediaPlugin(self)
        self.files = FilesPlugin(self)
        self.plugins = [
            BatteryPlugin(self),
            self.notifications,
            self.clipboard,
            self.ping,
            self.auth,
            self.media,
            self.files,
            PresencePlugin(self),
            AudioPlugin(self),
        ]

        # type -> plugins, built once so packet dispatch is a dict lookup.
        self._routes: dict[str, list] = {}
        for plugin in self.plugins:
            for packet_type in plugin.handles:
                self._routes.setdefault(packet_type, []).append(plugin)

        self.dbus = DBusService(self)
        self._loop = GLib.MainLoop()
        self._system_bus: Gio.DBusConnection | None = None
        self._reconnect_source: int | None = None
        self._device_id: str | None = None

        # Device id of the authenticated peer, for the LAN offer.
        self._peer_device_id: str | None = None

        # Set once the peer has proved it holds this device's secret.
        # Until then the link carries nothing but identity and auth.
        self.authenticated = False

        # State mirrored onto D-Bus for the shell extension.
        self.connected = False
        self.device_name = ""
        self.battery_level = -1
        self.battery_charging = False

    # ---------------------------------------------------------------- run

    def run(self) -> None:
        self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

        log.debug("config: %s", self.config)

        for plugin in self.plugins:
            plugin.start()

        self.transport.start()
        self.dbus.start()

        # Nudge any bonded phone immediately, then keep a slow retry going.
        self.try_reconnect()
        self._reconnect_source = GLib.timeout_add_seconds(
            RECONNECT_INTERVAL_SECONDS, self._reconnect_tick
        )

        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._quit)

        log.info("FedoraLink daemon ready")
        self._loop.run()

    def _quit(self) -> bool:
        log.info("shutting down")
        self.shutdown()
        self._loop.quit()
        return GLib.SOURCE_REMOVE

    def shutdown(self) -> None:
        self.alerter.stop()
        if self._reconnect_source is not None:
            GLib.source_remove(self._reconnect_source)
            self._reconnect_source = None
        for plugin in self.plugins:
            try:
                plugin.stop()
            except Exception:
                log.exception("plugin %s failed to stop", plugin.name)
        self.transport.stop()
        self.tcp.stop()
        self.dbus.stop()

    # ----------------------------------------------------------- outbound

    #: The two types that have to cross an unauthenticated link, because
    #: they are how it becomes authenticated.
    _PRE_AUTH_TYPES = (IDENTITY, AUTH)

    def send(self, packet_type: str, body: dict[str, Any] | None = None) -> bool:
        if not self.authenticated and packet_type not in self._PRE_AUTH_TYPES:
            # A plugin reacting to something local — a clipboard copy, a
            # battery report — must not leak it to a peer that hasn't
            # proved who it is.
            log.debug("refusing to send %s before authentication", packet_type)
            return False

        packet = make_packet(packet_type, body)
        # Prefer the LAN link when it's up; it is the same packets down a
        # pipe roughly two orders of magnitude faster.
        if self.tcp.connection is not None:
            return self.tcp.send(packet)
        return self.transport.send(packet)

    @property
    def on_lan(self) -> bool:
        return self.tcp.connection is not None

    @property
    def pending_bytes(self) -> int:
        """How much is queued on the active link, for back-pressure."""
        connection = self.tcp.connection or self.transport.connection
        return connection.pending_bytes if connection is not None else 0

    # ------------------------------------------------------ connection io

    def _on_connected(self, connection: Connection) -> None:
        self.connected = True
        self.authenticated = False
        self.device_name = connection.device_name

        self.send(
            IDENTITY,
            {
                "deviceName": GLib.get_host_name(),
                "deviceType": "desktop",
                "deviceId": self.device_id(),
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": [p.name for p in self.plugins],
            },
        )

        for plugin in self.plugins:
            try:
                plugin.on_connected(connection)
            except Exception:
                log.exception("plugin %s failed on connect", plugin.name)

        self.dbus.emit_changed()

    def _on_disconnected(self, connection: Connection) -> None:
        self.connected = False
        self.authenticated = False
        self._peer_device_id = None
        # No Bluetooth session means no LAN session either: the LAN link's
        # whole claim to trust came from that handshake.
        self.tcp.stop()
        self.device_name = ""
        self.battery_level = -1
        self.battery_charging = False

        for plugin in self.plugins:
            try:
                plugin.on_disconnected(connection)
            except Exception:
                log.exception("plugin %s failed on disconnect", plugin.name)

        self.dbus.emit_changed()

    def _on_packet(self, packet: dict[str, Any]) -> None:
        packet_type = packet["type"]

        if packet_type == IDENTITY:
            self._on_identity(packet["body"])
            return

        if not self.authenticated and packet_type != AUTH:
            # The gate. A peer that hasn't proved who it is gets no
            # clipboard, no notifications, nothing.
            log.warning("dropping %s from an unauthenticated peer", packet_type)
            return

        handlers = self._routes.get(packet_type)
        if not handlers:
            log.debug("no plugin handles %s", packet_type)
            return

        for plugin in handlers:
            plugin.on_packet(packet)

    def _on_identity(self, body: dict[str, Any]) -> None:
        name = body.get("deviceName")
        if name:
            self.device_name = name
            self.dbus.emit_changed()

        version = body.get("protocolVersion")
        if not isinstance(version, int) or version < MIN_PROTOCOL_VERSION:
            # Nothing to degrade to: a version 1 peer has no secret, and
            # accepting it unauthenticated is exactly what this exists to
            # prevent. Say so clearly — "update your phone app" is a much
            # better message than a silent disconnect.
            log.error(
                "%s speaks protocol %r; %d or newer is required. Update the "
                "FedoraLink app on the phone.",
                self.device_name or "peer", version, MIN_PROTOCOL_VERSION,
            )
            self.drop_connection("peer protocol too old")
            return

        log.info("handshake with %s (protocol %d)", self.device_name, version)
        device_id = body.get("deviceId")
        self._peer_device_id = device_id if isinstance(device_id, str) else None
        self.auth.begin(device_id, self.device_name)

    def device_id(self) -> str:
        """A stable identifier for this desktop.

        machine-id is per-install and survives a hostname change, which is
        what the phone needs to recognise us again. Hashed rather than sent
        raw: it is a known fingerprinting vector, and the phone only needs
        it to be stable, not meaningful.
        """
        if self._device_id is None:
            try:
                raw = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
            except OSError:
                raw = GLib.get_host_name()
            self._device_id = hashlib.sha256(
                f"fedoralink:{raw}".encode()
            ).hexdigest()[:32]
        return self._device_id

    def set_authenticated(self, value: bool) -> None:
        if self.authenticated == value:
            return
        self.authenticated = value
        self.dbus.emit_changed()

        if value:
            self._offer_lan_upgrade()

    def _offer_lan_upgrade(self) -> None:
        """Invite the phone onto a LAN link, if there's one to be had.

        Only ever called after Bluetooth authentication, so the address and
        nonce travel over a channel the peer has already proved itself on —
        which is why this needs no discovery protocol and has nothing to
        spoof.
        """
        if not self.config["lan_transport"]:
            return

        device_id = self._peer_device_id
        if device_id is None:
            return

        # The socket exists only while a phone is authenticated, so there
        # is nothing listening when none is around.
        port = self.tcp.start()
        if port is None:
            return

        hosts = local_addresses()
        if not hosts:
            log.debug("no LAN address to offer")
            self.tcp.stop()
            return

        nonce = self.tcp.new_offer(device_id)
        self.send(UPGRADE, {"hosts": hosts, "port": port, "nonce": nonce})
        log.info("offered a LAN link at %s:%d", hosts[0], port)

    def _on_tcp_connected(self, connection) -> None:
        log.info("packets now travel over the LAN link")
        self.dbus.emit_changed()

    def _on_tcp_disconnected(self, connection) -> None:
        # Bluetooth is still underneath, so this is a downgrade rather than
        # a disconnection. Offer again in case it was a transient blip.
        log.info("LAN link dropped; falling back to Bluetooth")
        self.dbus.emit_changed()
        if self.authenticated:
            self._offer_lan_upgrade()

    def forget_devices(self) -> int:
        """Revoke every enrolled device and hang up on the current one.

        Bluetooth pairing is left alone: this is about which devices this
        daemon trusts, which is a separate question from which ones can
        reach it.
        """
        store = self.auth.store
        count = 0
        for device_id in list(store.known_devices()):
            if store.revoke(device_id):
                count += 1

        if self.connected:
            self.drop_connection("device trust revoked")

        log.info("forgot %d device(s)", count)
        return count

    def drop_connection(self, reason: str) -> None:
        """Hang up. Used when a peer fails or refuses authentication."""
        log.info("dropping the link: %s", reason)
        self.authenticated = False
        self.transport.disconnect()

    # --------------------------------------------------------------- state

    def set_battery(self, level: int | None, charging: bool) -> None:
        new_level = -1 if level is None else level
        if new_level == self.battery_level and charging == self.battery_charging:
            return
        self.battery_level = new_level
        self.battery_charging = charging
        self.dbus.emit_changed()

    # ----------------------------------------------------------- reconnect

    def _reconnect_tick(self) -> bool:
        if not self.connected:
            self.try_reconnect()
        return GLib.SOURCE_CONTINUE

    def try_reconnect(self) -> None:
        """Ask BlueZ to open our profile on every bonded device.

        Devices that aren't running FedoraLink simply fail, which is
        cheap and harmless.
        """
        if self.connected or self._system_bus is None:
            return

        try:
            result = self._system_bus.call_sync(
                "org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
                "GetManagedObjects", None,
                GLib.VariantType("(a{oa{sa{sv}}})"),
                Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.debug("could not enumerate BlueZ objects: %s", exc)
            return

        for path, interfaces in result.unpack()[0].items():
            device = interfaces.get("org.bluez.Device1")
            if not device or not device.get("Paired"):
                continue
            # Only bother devices that advertised our service while bonded.
            uuids = [u.lower() for u in device.get("UUIDs", [])]
            if SERVICE_UUID.lower() not in uuids:
                continue

            log.debug("attempting outbound connect to %s", path)
            self._system_bus.call(
                "org.bluez", path, "org.bluez.Device1", "ConnectProfile",
                GLib.Variant("(s)", (SERVICE_UUID,)),
                None, Gio.DBusCallFlags.NONE, 15000, None,
                self._on_connect_profile_done, path,
            )

    def _on_connect_profile_done(
        self, bus: Gio.DBusConnection, result: Gio.AsyncResult, path: str
    ) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error as exc:
            # Expected whenever the phone is out of range or its service
            # isn't running. Not worth surfacing above debug.
            log.debug("ConnectProfile on %s failed: %s", path, exc.message)
