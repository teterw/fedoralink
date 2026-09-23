"""Mirror phone notifications onto the desktop, with two-way dismissal."""

from __future__ import annotations

import logging
from typing import Any

from gi.repository import Gio, GLib

from ..protocol import NOTIFICATION, NOTIFICATION_ACTION, NOTIFICATION_DISMISS
from . import Plugin

log = logging.getLogger(__name__)

FDO_BUS = "org.freedesktop.Notifications"
FDO_PATH = "/org/freedesktop/Notifications"
FDO_IFACE = "org.freedesktop.Notifications"

# org.freedesktop.Notifications close reasons
REASON_DISMISSED_BY_USER = 2

DISMISS_ACTION = "fedoralink-dismiss"
REPLY_ACTION = "fedoralink-reply"


class NotificationPlugin(Plugin):
    name = "notification"
    handles = (NOTIFICATION, NOTIFICATION_DISMISS, NOTIFICATION_ACTION)

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        self._bus: Gio.DBusConnection | None = None
        self._closed_sub: int | None = None
        self._action_sub: int | None = None

        # remote notification key -> local freedesktop notification id
        self._remote_to_local: dict[str, int] = {}
        self._local_to_remote: dict[int, str] = {}

        # Keys the phone says carry a reply action, so Reply is only
        # offered where it can actually deliver something.
        self._repliable: set[str] = set()

        # local id -> a label for the reply dialog, so it can say what it
        # is replying to.
        self._titles: dict[int, str] = {}

        # Local ids we closed ourselves because the phone told us to. The
        # resulting NotificationClosed signal must not be echoed back, or
        # the two sides ping-pong dismissals forever.
        self._self_closed: set[int] = set()

        # Local id -> callback, for notifications this daemon raised that
        # carry buttons of their own (device approval, for one). Kept apart
        # from the mirror maps: these aren't mirrors of anything, and
        # answering one must not send a dismissal to the phone.
        self._own_actions: dict[int, Any] = {}

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
        self._repliable.clear()
        self._titles.clear()

    def on_packet(self, packet: dict[str, Any]) -> None:
        if packet["type"] == NOTIFICATION:
            self._mirror(packet["body"])
        elif packet["type"] == NOTIFICATION_ACTION:
            self._on_remote_action(packet["body"])
        else:
            self._dismiss_locally(packet["body"].get("key"))

    def _on_remote_action(self, body: dict[str, Any]) -> None:
        if body.get("action") != "reply-failed":
            return
        # A silently dropped reply is the worst outcome here — the user
        # believes they answered someone, and they didn't.
        reason = body.get("reason") or "the phone could not deliver it"
        self.show_local(summary="Reply not delivered", body=str(reason), urgent=True)

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

        actions = [DISMISS_ACTION, "Dismiss on phone"]
        if body.get("canReply"):
            self._repliable.add(key)
            # First in the list, so it reads as the primary button.
            actions = [REPLY_ACTION, "Reply", *actions]
        else:
            # An app can lose its reply action between updates; don't leave
            # a button behind with nothing to deliver through.
            self._repliable.discard(key)

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
                actions,
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
        self._titles[local_id] = f"{app_name}: {title}" if title else app_name

    def ask(
        self,
        summary: str,
        body: str,
        actions: list[tuple[str, str]],
        on_action,
        *,
        urgent: bool = True,
    ) -> int | None:
        """Post a notification with buttons and route the answer back.

        `actions` is [(key, label)]. `on_action` is called with the chosen
        key, or with None if the notification is closed without an answer
        — a dialog the user swipes away has to resolve, not hang.
        """
        if self._bus is None:
            return None

        hints = {
            "desktop-entry": GLib.Variant("s", "org.fedoralink.FedoraLink"),
        }
        if urgent:
            # Critical, so it survives Do Not Disturb and doesn't time out
            # before anyone reads it.
            hints["urgency"] = GLib.Variant("y", 2)

        flat: list[str] = []
        for key, label in actions:
            flat += [key, label]

        args = GLib.Variant(
            "(susssasa{sv}i)",
            (
                "FedoraLink", 0, "phone-symbolic", summary, body, flat, hints, 0,
            ),
        )

        try:
            result = self._bus.call_sync(
                FDO_BUS, FDO_PATH, FDO_IFACE, "Notify", args,
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.warning("could not ask the user: %s", exc)
            return None

        local_id = result.unpack()[0]
        self._own_actions[local_id] = on_action
        return local_id

    def show_progress(
        self, local_id: int | None, summary: str, body: str
    ) -> int | None:
        """Post or update a progress notification in place.

        Passing the previous id back as `replaces_id` is what stops a
        transfer stacking one notification per chunk.
        """
        if self._bus is None:
            return None

        args = GLib.Variant(
            "(susssasa{sv}i)",
            (
                "FedoraLink",
                local_id or 0,
                "phone-symbolic",
                summary,
                body,
                [],
                {
                    "desktop-entry": GLib.Variant(
                        "s", "org.fedoralink.FedoraLink"
                    ),
                    # Transient: progress is not worth keeping in the tray
                    # after the fact.
                    "transient": GLib.Variant("b", True),
                },
                -1,
            ),
        )

        try:
            result = self._bus.call_sync(
                FDO_BUS, FDO_PATH, FDO_IFACE, "Notify", args,
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.debug("progress notification failed: %s", exc)
            return local_id

        return result.unpack()[0]

    def cancel_ask(self, local_id: int | None) -> None:
        """Withdraw a question whose answer no longer matters."""
        if local_id is None:
            return
        if self._own_actions.pop(local_id, None) is not None:
            self._close_local(local_id)

    def _dismiss_locally(self, key: str | None) -> None:
        """The phone dismissed it — clear our mirror without echoing back."""
        if not key:
            return
        local_id = self._remote_to_local.pop(key, None)
        if local_id is None:
            return
        self._local_to_remote.pop(local_id, None)
        self._titles.pop(local_id, None)
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
            self._own_actions.pop(local_id, None)
            return

        # One of ours, dismissed without an answer. Resolve it as "no
        # answer" rather than leaving the asker waiting forever.
        callback = self._own_actions.pop(local_id, None)
        if callback is not None:
            callback(None)
            return

        key = self._local_to_remote.pop(local_id, None)
        if key is None:
            return
        self._remote_to_local.pop(key, None)
        # Kept in step with the other two maps: without this, a phone that
        # posts notifications all day leaves an entry here for every one of
        # them, for the life of the connection.
        self._titles.pop(local_id, None)

        # Only propagate a deliberate dismissal. A timeout just means it
        # scrolled off the desktop; the phone's copy should survive that.
        if reason == REASON_DISMISSED_BY_USER:
            self.send(NOTIFICATION_DISMISS, {"key": key})

    def _on_action_invoked(
        self, _conn, _sender, _path, _iface, _signal, params, _user_data
    ) -> None:
        local_id, action = params.unpack()

        callback = self._own_actions.pop(local_id, None)
        if callback is not None:
            # Answered. Take it off screen so a stale question can't be
            # answered twice.
            self._close_local(local_id)
            callback(action)
            return

        key = self._local_to_remote.get(local_id)
        if key is None:
            return

        if action == DISMISS_ACTION:
            self.send(NOTIFICATION_ACTION, {"key": key, "action": "dismiss"})
        elif action == REPLY_ACTION:
            # GNOME Shell's notification server has no inline reply — its
            # capabilities are actions/body/body-markup/icon-static/
            # persistence/sound, with no NotificationReplied signal. So the
            # shell extension puts the text box on screen instead. It is
            # the compositor, which is the same reason it owns clipboard
            # I/O. See ReplyRequested in dbus_service.py.
            self.daemon.dbus.emit_reply_requested(
                key, self._titles.get(local_id, "")
            )

    def reply(self, key: str, text: str) -> None:
        """Deliver a reply the shell extension collected from the user."""
        if key not in self._repliable:
            log.warning("ignoring reply to %s, which is not repliable", key)
            return
        if not text:
            return
        self.send(NOTIFICATION_ACTION, {"key": key, "action": "reply", "text": text})

    def show_local(
        self,
        summary: str,
        body: str = "",
        *,
        urgent: bool = False,
        sound_name: str | None = None,
    ) -> None:
        """Post a desktop notification that isn't a mirror of anything.

        `urgent` marks it critical, which is what keeps it on screen under
        Do Not Disturb. `sound_name` is a freedesktop sound-theme name for
        the notification server to play — the fallback for making noise
        when canberra-gtk-play isn't installed.
        """
        if self._bus is None:
            return

        hints = {
            "desktop-entry": GLib.Variant("s", "org.fedoralink.FedoraLink"),
        }
        if urgent:
            hints["urgency"] = GLib.Variant("y", 2)
        if sound_name:
            hints["sound-name"] = GLib.Variant("s", sound_name)

        args = GLib.Variant(
            "(susssasa{sv}i)",
            (
                "FedoraLink", 0, "phone-symbolic", summary, body, [],
                hints,
                # Critical notifications ignore the timeout anyway; for the
                # rest, let the server decide.
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
