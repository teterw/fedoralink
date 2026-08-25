"""Session-bus interface consumed by the GNOME Shell extension.

The extension is a separate process that GNOME reloads on lock/unlock and
on every screen resize. Keeping the Bluetooth link in this daemon instead
means none of that churn drops the connection — the extension is only a
view onto state that lives here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from gi.repository import Gio, GLib

if TYPE_CHECKING:  # pragma: no cover
    from .daemon import Daemon

log = logging.getLogger(__name__)

BUS_NAME = "org.fedoralink.Daemon"
OBJECT_PATH = "/org/fedoralink/Daemon"
INTERFACE = "org.fedoralink.Daemon"

INTROSPECTION = """
<node>
  <interface name='org.fedoralink.Daemon'>
    <method name='Ping'/>
    <method name='SendClipboard'/>
    <method name='Reconnect'/>
    <!-- The shell extension reads and writes the selection on our behalf;
         see plugins/clipboard.py for why the daemon can't do it itself. -->
    <method name='SetClipboard'>
      <arg name='content' type='s' direction='in'/>
      <arg name='force' type='b' direction='in'/>
    </method>
    <method name='SetClipboardBridge'>
      <arg name='active' type='b' direction='in'/>
    </method>
    <signal name='ClipboardChanged'>
      <arg name='content' type='s'/>
    </signal>
    <property name='Connected' type='b' access='read'/>
    <property name='DeviceName' type='s' access='read'/>
    <property name='BatteryLevel' type='i' access='read'/>
    <property name='BatteryCharging' type='b' access='read'/>
  </interface>
</node>
"""


class DBusService:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self._bus: Gio.DBusConnection | None = None
        self._owner_id: int | None = None
        self._reg_id: int | None = None

        # Set while a shell extension is registered to do clipboard I/O.
        self.clipboard_bridge_active = False
        self._bridge_sender: str | None = None
        self._bridge_watch_id: int | None = None

    def start(self) -> None:
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            None,
            self._on_name_lost,
        )

    def stop(self) -> None:
        self._clear_bridge_watch()
        self.clipboard_bridge_active = False
        if self._bus is not None and self._reg_id is not None:
            self._bus.unregister_object(self._reg_id)
            self._reg_id = None
        if self._owner_id is not None:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = None

    def _on_bus_acquired(self, connection: Gio.DBusConnection, _name: str) -> None:
        self._bus = connection
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        self._reg_id = connection.register_object(
            OBJECT_PATH,
            node.interfaces[0],
            self._handle_method_call,
            self._handle_get_property,
            None,
        )
        log.info("claimed %s on the session bus", BUS_NAME)

    def _on_name_lost(self, _connection, _name: str) -> None:
        # Almost always a second daemon instance. Two would fight over the
        # RFCOMM profile, so bow out rather than run half-broken.
        log.error("could not own %s — is another daemon running?", BUS_NAME)
        self.daemon.shutdown()

    def _handle_method_call(
        self, _conn, sender, _path, _iface, method, params, invocation
    ) -> None:
        if method == "Ping":
            self.daemon.ping.ring_phone()
        elif method == "SendClipboard":
            self.daemon.clipboard.send_current()
        elif method == "SetClipboard":
            content, force = params.unpack()
            self.daemon.clipboard.set_from_shell(content, force)
        elif method == "SetClipboardBridge":
            (active,) = params.unpack()
            self._set_bridge(sender, active)
        elif method == "Reconnect":
            self.daemon.try_reconnect()
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )
            return
        invocation.return_value(None)

    def _handle_get_property(
        self, _conn, _sender, _path, _iface, prop: str
    ) -> GLib.Variant | None:
        daemon = self.daemon
        if prop == "Connected":
            return GLib.Variant("b", daemon.connected)
        if prop == "DeviceName":
            return GLib.Variant("s", daemon.device_name)
        if prop == "BatteryLevel":
            return GLib.Variant("i", daemon.battery_level)
        if prop == "BatteryCharging":
            return GLib.Variant("b", daemon.battery_charging)
        return None

    def _set_bridge(self, sender: str, active: bool) -> None:
        """Register the extension as the clipboard's reader and writer.

        The extension has no bus name of its own, so we watch its unique
        name: if the shell dies or the extension is disabled without a
        clean unregister, we fall back to wl-copy instead of emitting a
        signal nobody is listening for.
        """
        self._clear_bridge_watch()

        self.clipboard_bridge_active = active
        self._bridge_sender = sender if active else None

        if active and self._bus is not None:
            self._bridge_watch_id = Gio.bus_watch_name_on_connection(
                self._bus,
                sender,
                Gio.BusNameWatcherFlags.NONE,
                None,
                self._on_bridge_vanished,
            )
        log.info("clipboard bridge %s", "registered" if active else "released")

    def _on_bridge_vanished(self, _connection, _name: str) -> None:
        log.info("clipboard bridge went away, falling back to wl-copy")
        self.clipboard_bridge_active = False
        self._bridge_sender = None

    def _clear_bridge_watch(self) -> None:
        if self._bridge_watch_id is not None:
            Gio.bus_unwatch_name(self._bridge_watch_id)
            self._bridge_watch_id = None

    def emit_clipboard(self, content: str) -> None:
        """Ask the extension to put `content` on the desktop clipboard."""
        if self._bus is None:
            return
        try:
            self._bus.emit_signal(
                self._bridge_sender,
                OBJECT_PATH,
                INTERFACE,
                "ClipboardChanged",
                GLib.Variant("(s)", (content,)),
            )
        except GLib.Error as exc:
            log.debug("ClipboardChanged emit failed: %s", exc)

    def emit_changed(self) -> None:
        """Push current state to the shell extension."""
        if self._bus is None:
            return

        changed = {
            "Connected": GLib.Variant("b", self.daemon.connected),
            "DeviceName": GLib.Variant("s", self.daemon.device_name),
            "BatteryLevel": GLib.Variant("i", self.daemon.battery_level),
            "BatteryCharging": GLib.Variant("b", self.daemon.battery_charging),
        }

        try:
            self._bus.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (INTERFACE, changed, [])),
            )
        except GLib.Error as exc:
            log.debug("PropertiesChanged emit failed: %s", exc)
