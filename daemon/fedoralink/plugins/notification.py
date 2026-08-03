"""Mirror phone notifications onto the desktop, with two-way dismissal."""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import Plugin
from ..protocol import NOTIFICATION, NOTIFICATION_ACTION, NOTIFICATION_DISMISS

log = logging.getLogger(__name__)

FDO_BUS = "org.freedesktop.Notifications"
FDO_PATH = "/org/freedesktop/Notifications"
FDO_IFACE = "org.freedesktop.Notifications"

# org.freedesktop.Notifications close reasons
REASON_DISMISSED_BY_USER = 2

DISMISS_ACTION = "fedoralink-dismiss"


class NotificationPlugin(Plugin):
    name = "notification"
    handles = (NOTIFICATION, NOTIFICATION_DISMISS)

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        self._bus: Gio.DBusConnection | None = None
        self._closed_sub: int | None = None
        self._action_sub: int | None = None

        # remote notification key -> local freedesktop notification id
        self._remote_to_local: dict[str, int] = {}
        self._local_to_remote: dict[int, str] = {}

        # Local ids we closed ourselves because the phone told us to. The
        # resulting NotificationClosed signal must not be echoed back, or
        # the two sides ping-pong dismissals forever.
        self._self_closed: set[int] = set()

    def start(self) -> None:
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        self._closed_sub = self._bus.signal_subscribe(
            None, FDO_IFACE, "NotificationClosed", FDO_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_notification_closed, None,
        )
        self._action_sub = self._bus.signal_subscribe(
            None, FDO_IFACE, "ActionInvoked", FDO_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_action_invoked, None,
        )

    def stop(self) -> None:
        if self._bus is None:
            return
        for sub in (self._closed_sub, self._action_sub):
            if sub is not None:
                self._bus.signal_unsubscribe(sub)
        self._closed_sub = self._action_sub = None

    def on_disconnected(self, connection) -> None:
        # Close the mirrors too. Leaving them on screen implies a live link.
        for local_id in list(self._local_to_remote):
            self._close_local(local_id)
        self._remote_to_local.clear()
        self._local_to_remote.clear()

    def on_packet(self, packet: dict[str, Any]) -> None:
        if packet["type"] == NOTIFICATION:
            self._mirror(packet["body"])
        else:
            self._dismiss_locally(packet["body"].get("key"))

    def _mirror(self, body: dict[str, Any]) -> None:
        key = body.get("key")
        if not key:
            log.warning("notification without a key; ignoring")
            return

        app_name = body.get("appName") or "Phone"
        title = body.get("title") or app_name
        text = body.get("text") or ""

        # Reusing the existing id makes an updated notification replace the
        # old one in place instead of stacking a duplicate.
        replaces_id = self._remote_to_local.get(key, 0)

        hints = {
            # Groups mirrored notifications under FedoraLink in GNOME.
            "desktop-entry": GLib.Variant("s", "org.fedoralink.FedoraLink"),
            "urgency": GLib.Variant("y", 1),
        }

        args = GLib.Variant(
            "(susssasa{sv}i)",
            (
                app_name,
                replaces_id,
                "phone-symbolic",
                title,
                text,
                [DISMISS_ACTION, "Dismiss on phone"],
                hints,
                # 0 would mean "never expire" — but these are mirrors of
                # something already on the phone, so let them time out.
                -1,
            ),
        )

        try:
            result = self._bus.call_sync(
                FDO_BUS, FDO_PATH, FDO_IFACE, "Notify", args,
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.warning("could not post notification: %s", exc)
            return

        local_id = result.unpack()[0]
        self._remote_to_local[key] = local_id
        self._local_to_remote[local_id] = key

    def _dismiss_locally(self, key: str | None) -> None:
        """The phone dismissed it — clear our mirror without echoing back."""
        if not key:
            return
        local_id = self._remote_to_local.pop(key, None)
        if local_id is None:
            return
        self._local_to_remote.pop(local_id, None)
        self._close_local(local_id)

    def _close_local(self, local_id: int) -> None:
        self._self_closed.add(local_id)
        try:
            self._bus.call_sync(
                FDO_BUS, FDO_PATH, FDO_IFACE, "CloseNotification",
                GLib.Variant("(u)", (local_id,)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.debug("CloseNotification(%d) failed: %s", local_id, exc)
            self._self_closed.discard(local_id)

    def _on_notification_closed(
        self, _conn, _sender, _path, _iface, _signal, params, _user_data
    ) -> None:
        local_id, reason = params.unpack()

        if local_id in self._self_closed:
            self._self_closed.discard(local_id)
            return

        key = self._local_to_remote.pop(local_id, None)
        if key is None:
            return
        self._remote_to_local.pop(key, None)

        # Only propagate a deliberate dismissal. A timeout just means it
        # scrolled off the desktop; the phone's copy should survive that.
        if reason == REASON_DISMISSED_BY_USER:
            self.send(NOTIFICATION_DISMISS, {"key": key})

    def _on_action_invoked(
        self, _conn, _sender, _path, _iface, _signal, params, _user_data
    ) -> None:
        local_id, action = params.unpack()
        key = self._local_to_remote.get(local_id)
        if key is None or action != DISMISS_ACTION:
            return
        self.send(NOTIFICATION_ACTION, {"key": key, "action": "dismiss"})

    def show_local(self, summary: str, body: str = "") -> None:
        """Post a desktop notification that isn't a mirror of anything."""
        if self._bus is None:
            return
        args = GLib.Variant(
            "(susssasa{sv}i)",
            (
                "FedoraLink", 0, "phone-symbolic", summary, body, [],
                {"desktop-entry": GLib.Variant("s", "org.fedoralink.FedoraLink")},
                -1,
            ),
        )
        try:
            self._bus.call_sync(
                FDO_BUS, FDO_PATH, FDO_IFACE, "Notify", args,
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.debug("show_local failed: %s", exc)
