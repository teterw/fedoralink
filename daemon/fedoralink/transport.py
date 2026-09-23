"""RFCOMM transport built on BlueZ's Profile1 API.

We register a Bluetooth profile with BlueZ rather than opening a raw
socket ourselves. BlueZ then owns the SDP record, which is what lets the
phone find our RFCOMM channel by UUID instead of us hardcoding one.

The PC is the server: it registers the profile and waits. The phone is
the client and does the reconnecting, because the phone is the device
that actually moves in and out of range.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib

from .protocol import SERVICE_UUID, PacketReader, ProtocolError, serialize

log = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
BLUEZ_PATH = "/org/bluez"
PROFILE_MANAGER_IFACE = "org.bluez.ProfileManager1"
PROFILE_OBJECT_PATH = "/org/fedoralink/Profile"

PROFILE_INTROSPECTION = """
<node>
  <interface name='org.bluez.Profile1'>
    <method name='Release'/>
    <method name='NewConnection'>
      <arg type='o' name='device' direction='in'/>
      <arg type='h' name='fd' direction='in'/>
      <arg type='a{sv}' name='fd_properties' direction='in'/>
    </method>
    <method name='RequestDisconnection'>
      <arg type='o' name='device' direction='in'/>
    </method>
  </interface>
</node>
"""

READ_CHUNK = 4096


class Connection:
    """One live RFCOMM link to a phone."""

    def __init__(
        self,
        sock: socket.socket,
        device_path: str,
        device_name: str,
        on_packet: Callable[[dict[str, Any]], None],
        on_close: Callable[[Connection], None],
    ) -> None:
        self.sock = sock
        self.device_path = device_path
        self.device_name = device_name
        self._on_packet = on_packet
        self._on_close = on_close
        self._reader = PacketReader()
        self._outbox = bytearray()
        self._closed = False

        self.sock.setblocking(False)
        self._in_source = GLib.unix_fd_add_full(
            GLib.PRIORITY_DEFAULT,
            self.sock.fileno(),
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._on_readable,
        )
        self._out_source: int | None = None

    @property
    def pending_bytes(self) -> int:
        """Bytes queued but not yet written to the socket.

        RFCOMM drains at roughly 200 KB/s, so a file pump that ignored this
        would queue the whole file in memory in a second or two.
        """
        return len(self._outbox)

    def _on_readable(self, _fd: int, condition: GLib.IOCondition) -> bool:
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            log.info("link to %s hung up", self.device_name)
            self.close()
            return False

        try:
            chunk = self.sock.recv(READ_CHUNK)
        except BlockingIOError:
            return True
        except OSError as exc:
            log.info("read from %s failed: %s", self.device_name, exc)
            self.close()
            return False

        if not chunk:
            self.close()
            return False

        # A malformed packet is survivable: framing is still intact, so log
        # it and keep the link up rather than dropping the connection.
        try:
            for packet in self._reader.feed(chunk):
                try:
                    self._on_packet(packet)
                except Exception:
                    log.exception("plugin raised handling %s", packet.get("type"))
        except ProtocolError as exc:
            log.warning("discarding bad packet from %s: %s", self.device_name, exc)

        return True

    def send(self, packet: dict[str, Any]) -> None:
        if self._closed:
            return
        self._outbox.extend(serialize(packet))
        self._flush()

    def _flush(self) -> None:
        while self._outbox:
            try:
                sent = self.sock.send(self._outbox)
            except BlockingIOError:
                # Kernel buffer is full — RFCOMM is slow, so this is normal
                # under a burst. Wait for writability and resume.
                self._watch_writable()
                return
            except OSError as exc:
                log.info("write to %s failed: %s", self.device_name, exc)
                self.close()
                return
            del self._outbox[:sent]

        self._unwatch_writable()

    def _watch_writable(self) -> None:
        if self._out_source is None:
            self._out_source = GLib.unix_fd_add_full(
                GLib.PRIORITY_DEFAULT,
                self.sock.fileno(),
                GLib.IOCondition.OUT,
                self._on_writable,
            )

    def _unwatch_writable(self) -> None:
        if self._out_source is not None:
            GLib.source_remove(self._out_source)
            self._out_source = None

    def _on_writable(self, _fd: int, _condition: GLib.IOCondition) -> bool:
        if self._closed:
            return False
        self._flush()
        return self._out_source is not None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        self._unwatch_writable()
        if self._in_source is not None:
            GLib.source_remove(self._in_source)
            self._in_source = None

        try:
            self.sock.close()
        except OSError:
            pass

        self._on_close(self)


class RfcommTransport:
    """Registers the FedoraLink profile and hands out Connections."""

    def __init__(
        self,
        on_connected: Callable[[Connection], None],
        on_disconnected: Callable[[Connection], None],
        on_packet: Callable[[dict[str, Any]], None],
    ) -> None:
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_packet = on_packet

        self.connection: Connection | None = None
        self._bus: Gio.DBusConnection | None = None
        self._reg_id: int | None = None

    def start(self) -> None:
        self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        node = Gio.DBusNodeInfo.new_for_xml(PROFILE_INTROSPECTION)

        self._reg_id = self._bus.register_object(
            PROFILE_OBJECT_PATH,
            node.interfaces[0],
            self._handle_method_call,
            None,
            None,
        )

        # Plain dict of Variants — nesting a pre-built a{sv} Variant inside
        # a tuple Variant makes PyGObject try to iterate it as a mapping.
        options = {
            "Name": GLib.Variant("s", "FedoraLink"),
            "Role": GLib.Variant("s", "server"),
            # Channel 0 lets BlueZ pick and advertise one over SDP, so
            # the phone resolves it by UUID and we never hardcode.
            "Channel": GLib.Variant("q", 0),
            "RequireAuthentication": GLib.Variant("b", True),
            # Authorization would prompt on every reconnect; bonding
            # already gates who may reach us.
            "RequireAuthorization": GLib.Variant("b", False),
            "AutoConnect": GLib.Variant("b", True),
        }

        self._bus.call_sync(
            BLUEZ_BUS,
            BLUEZ_PATH,
            PROFILE_MANAGER_IFACE,
            "RegisterProfile",
            GLib.Variant("(osa{sv})", (PROFILE_OBJECT_PATH, SERVICE_UUID, options)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        log.info("registered RFCOMM profile %s", SERVICE_UUID)

    def stop(self) -> None:
        if self.connection:
            self.connection.close()

        if self._bus and self._reg_id is not None:
            try:
                self._bus.call_sync(
                    BLUEZ_BUS,
                    BLUEZ_PATH,
                    PROFILE_MANAGER_IFACE,
                    "UnregisterProfile",
                    GLib.Variant("(o)", (PROFILE_OBJECT_PATH,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    -1,
                    None,
                )
            except GLib.Error as exc:
                log.debug("UnregisterProfile failed on shutdown: %s", exc)
            self._bus.unregister_object(self._reg_id)
            self._reg_id = None

    def send(self, packet: dict[str, Any]) -> bool:
        if self.connection is None:
            return False
        self.connection.send(packet)
        return True

    def disconnect(self) -> None:
        """Hang up on the current peer.

        Used when a device fails authentication: the socket has to go, or
        it sits there able to keep trying. Closing fires the same close
        path a dropped link does, so on_disconnected still runs and the
        plugins still clean up.
        """
        if self.connection is not None:
            self.connection.close()

    def _handle_method_call(
        self,
        _conn: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        method: str,
        params: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method == "NewConnection":
            device_path, fd_index, _props = params.unpack()
            self._accept(device_path, fd_index, invocation)
        elif method == "RequestDisconnection":
            if self.connection:
                self.connection.close()
            invocation.return_value(None)
        elif method == "Release":
            invocation.return_value(None)
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", method
            )

    def _accept(
        self, device_path: str, fd_index: int, invocation: Gio.DBusMethodInvocation
    ) -> None:
        fd_list = invocation.get_message().get_unix_fd_list()
        if fd_list is None:
            invocation.return_dbus_error(
                "org.bluez.Error.Rejected", "no file descriptor in message"
            )
            return

        try:
            # .get() dups the fd, so the socket below owns its own copy.
            fd = fd_list.get(fd_index)
        except GLib.Error as exc:
            invocation.return_dbus_error("org.bluez.Error.Rejected", str(exc))
            return

        # Only one phone at a time. A new link supersedes a stale one that
        # the kernel hasn't torn down yet.
        if self.connection is not None:
            log.info("replacing existing connection")
            self.connection.close()

        sock = socket.socket(
            family=socket.AF_BLUETOOTH,
            type=socket.SOCK_STREAM,
            proto=socket.BTPROTO_RFCOMM,
            fileno=fd,
        )

        name = self._device_alias(device_path)
        conn = Connection(
            sock=sock,
            device_path=device_path,
            device_name=name,
            on_packet=self._on_packet,
            on_close=self._handle_close,
        )
        self.connection = conn
        log.info("connected to %s", name)

        invocation.return_value(None)
        self._on_connected(conn)

    def _handle_close(self, conn: Connection) -> None:
        if self.connection is conn:
            self.connection = None
        self._on_disconnected(conn)

    def _device_alias(self, device_path: str) -> str:
        """Human-readable phone name, falling back to the MAC in the path."""
        try:
            result = self._bus.call_sync(
                BLUEZ_BUS,
                device_path,
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", ("org.bluez.Device1", "Alias")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            return result.unpack()[0]
        except GLib.Error:
            return device_path.rsplit("/", 1)[-1].replace("dev_", "").replace("_", ":")
