"""Daemon core: owns the transport, the plugins, and the shared state."""

from __future__ import annotations

import logging
import signal
from typing import Any

from gi.repository import Gio, GLib

from .dbus_service import DBusService
from .plugins.battery import BatteryPlugin
from .plugins.clipboard import ClipboardPlugin
from .plugins.notification import NotificationPlugin
from .plugins.ping import PingPlugin
from .protocol import IDENTITY, PROTOCOL_VERSION, SERVICE_UUID, make_packet
from .transport import Connection, RfcommTransport

log = logging.getLogger(__name__)

# How often the PC tries to reach a bonded phone that isn't connected.
# The phone reconnects far more aggressively; this is just a safety net
# for when its service was killed and restarted out of range.
RECONNECT_INTERVAL_SECONDS = 60


class Daemon:
    def __init__(self) -> None:
        self.transport = RfcommTransport(
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_packet=self._on_packet,
        )

        self.notifications = NotificationPlugin(self)
        self.clipboard = ClipboardPlugin(self)
        self.ping = PingPlugin(self)
        self.plugins = [
            BatteryPlugin(self),
            self.notifications,
            self.clipboard,
            self.ping,
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

        # State mirrored onto D-Bus for the shell extension.
        self.connected = False
        self.device_name = ""
        self.battery_level = -1
        self.battery_charging = False

    # ---------------------------------------------------------------- run

    def run(self) -> None:
        self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

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
        if self._reconnect_source is not None:
            GLib.source_remove(self._reconnect_source)
            self._reconnect_source = None
        for plugin in self.plugins:
            try:
                plugin.stop()
            except Exception:
                log.exception("plugin %s failed to stop", plugin.name)
        self.transport.stop()
        self.dbus.stop()

    # ----------------------------------------------------------- outbound

    def send(self, packet_type: str, body: dict[str, Any] | None = None) -> bool:
        return self.transport.send(make_packet(packet_type, body))

    # ------------------------------------------------------ connection io

    def _on_connected(self, connection: Connection) -> None:
        self.connected = True
        self.device_name = connection.device_name

        self.send(
            IDENTITY,
            {
                "deviceName": GLib.get_host_name(),
                "deviceType": "desktop",
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
            name = packet["body"].get("deviceName")
            if name:
                self.device_name = name
                self.dbus.emit_changed()
            log.info("handshake with %s", self.device_name)
            return

        handlers = self._routes.get(packet_type)
        if not handlers:
            log.debug("no plugin handles %s", packet_type)
            return

        for plugin in handlers:
            plugin.on_packet(packet)

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
