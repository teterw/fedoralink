"""Let the phone drive the pointer, as a trackpad.

**Why the portal and not uinput.** Writing to ``/dev/uinput`` is the short
path, and it is the wrong one here: it needs the user in the ``input``
group or a udev rule, neither of which Fedora sets up, so the feature would
silently do nothing for most people. ``org.freedesktop.portal.RemoteDesktop``
needs no group membership, no extra package and no root — it is how
remote-desktop tools inject input under Wayland, which has no unprivileged
synthetic-input path by design.

The cost is a consent dialog. It appears once per daemon session, and only
the first time the phone actually sends input: the portal session is
created lazily, so a user who never opens the trackpad is never asked.

Portal choreography, for anyone reading this later. Each of the first three
calls returns a Request object path and answers asynchronously on that
path's ``Response`` signal; only after ``Start`` succeeds can input be
sent, and ``Start`` is where the human is asked.

    CreateSession → SelectDevices(POINTER) → Start → NotifyPointer*
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from gi.repository import Gio, GLib

from ..protocol import INPUT
from . import Plugin

log = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REMOTE_DESKTOP = "org.freedesktop.portal.RemoteDesktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

# org.freedesktop.portal.RemoteDesktop DeviceType bitmask.
DEVICE_POINTER = 2

# Linux input event codes, which is what NotifyPointerButton speaks.
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

BUTTONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}

# Portal response codes.
RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1


class RemoteInputPlugin(Plugin):
    name = "input"
    handles = (INPUT,)

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        self._bus: Gio.DBusConnection | None = None
        self._session: str | None = None
        # Guards against firing three portal dialogs because three motion
        # events arrived before the first one finished.
        self._starting = False
        self._refused = False
        self._subscriptions: list[int] = []

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            log.warning("no session bus; remote input unavailable: %s", exc)

    def stop(self) -> None:
        self._close_session()
        self._bus = None

    def on_disconnected(self, connection) -> None:
        # Keep the portal session: reconnects are common and re-prompting
        # the user every time the phone blips would be unbearable.
        # A refusal is forgotten, though, so they can change their mind.
        self._refused = False

    # --------------------------------------------------------------- inbound

    def on_packet(self, packet: dict[str, Any]) -> None:
        if not self.daemon.config["remote_input"]:
            return

        body = packet["body"]
        kind = body.get("kind")

        if self._session is None:
            # Lazy: the first event is what triggers the consent dialog, so
            # someone who never opens the trackpad is never asked.
            self._ensure_session()
            # This event is dropped. A trackpad sends dozens a second, so
            # queueing it buys nothing but latency.
            return

        if kind == "motion":
            self._motion(body)
        elif kind == "button":
            self._button(body)
        elif kind == "scroll":
            self._scroll(body)
        else:
            log.debug("unknown input kind %r", kind)

    def _motion(self, body: dict[str, Any]) -> None:
        dx, dy = _number(body.get("dx")), _number(body.get("dy"))
        if dx is None or dy is None:
            return
        self._notify("NotifyPointerMotion", GLib.Variant("(oa{sv}dd)", (
            self._session, {}, dx, dy,
        )))

    def _button(self, body: dict[str, Any]) -> None:
        code = BUTTONS.get(body.get("button"))
        if code is None:
            log.debug("unknown button %r", body.get("button"))
            return
        pressed = 1 if body.get("pressed") else 0
        self._notify("NotifyPointerButton", GLib.Variant("(oa{sv}iu)", (
            self._session, {}, code, pressed,
        )))

    def _scroll(self, body: dict[str, Any]) -> None:
        dx, dy = _number(body.get("dx")) or 0.0, _number(body.get("dy")) or 0.0
        if dx == 0.0 and dy == 0.0:
            return
        self._notify("NotifyPointerAxis", GLib.Variant("(oa{sv}dd)", (
            self._session, {}, dx, dy,
        )))

    def _notify(self, method: str, args: GLib.Variant) -> None:
        if self._bus is None or self._session is None:
            return
        try:
            self._bus.call(
                PORTAL_BUS, PORTAL_PATH, REMOTE_DESKTOP, method, args,
                None, Gio.DBusCallFlags.NONE, 5000, None, None,
            )
        except GLib.Error as exc:
            log.warning("%s failed: %s", method, exc.message)
            # The compositor probably ended the session; next event will
            # start a new one.
            self._session = None

    # ---------------------------------------------------------- portal setup

    def _ensure_session(self) -> None:
        if self._bus is None or self._starting or self._refused:
            return
        self._starting = True
        log.info("asking the portal for pointer control")
        self._create_session()

    def _token(self) -> str:
        return f"fedoralink{secrets.token_hex(8)}"

    def _call_with_response(self, method: str, args: tuple, on_response) -> None:
        """Make a portal call and route its Response signal to a callback.

        Every request answers on its own object path, so the subscription
        has to be in place before the reply lands.
        """
        assert self._bus is not None
        token = self._token()
        options = {"handle_token": GLib.Variant("s", token)}

        if method == "CreateSession":
            options["session_handle_token"] = GLib.Variant("s", self._token())
            variant = GLib.Variant("(a{sv})", (options,))
        elif method == "SelectDevices":
            options["types"] = GLib.Variant("u", DEVICE_POINTER)
            variant = GLib.Variant("(oa{sv})", (args[0], options))
        else:  # Start
            variant = GLib.Variant("(osa{sv})", (args[0], "", options))

        subscription: list[int] = []

        def handler(_conn, _sender, _path, _iface, _signal, params, _user):
            code, results = params.unpack()
            if subscription:
                self._bus.signal_unsubscribe(subscription[0])
                if subscription[0] in self._subscriptions:
                    self._subscriptions.remove(subscription[0])
            on_response(code, results)

        # The request path is derivable from the token, so subscribe by it
        # rather than racing the method's return value.
        unique = self._bus.get_unique_name().removeprefix(":").replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{unique}/{token}"
        subscription.append(
            self._bus.signal_subscribe(
                PORTAL_BUS, REQUEST_IFACE, "Response", request_path, None,
                Gio.DBusSignalFlags.NONE, handler, None,
            )
        )
        self._subscriptions.append(subscription[0])

        self._bus.call(
            PORTAL_BUS, PORTAL_PATH, REMOTE_DESKTOP, method, variant,
            None, Gio.DBusCallFlags.NONE, 30000, None, self._on_call_done, method,
        )

    def _on_call_done(self, bus, result, method) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error as exc:
            log.warning("portal %s failed: %s", method, exc.message)
            self._starting = False

    def _create_session(self) -> None:
        def done(code, results):
            if code != RESPONSE_SUCCESS:
                self._fail("could not create a portal session", code)
                return
            session = results.get("session_handle")
            if not session:
                self._fail("portal returned no session handle", code)
                return
            self._select_devices(session)

        self._call_with_response("CreateSession", (), done)

    def _select_devices(self, session: str) -> None:
        def done(code, _results):
            if code != RESPONSE_SUCCESS:
                self._fail("portal refused pointer control", code)
                return
            self._start(session)

        self._call_with_response("SelectDevices", (session,), done)

    def _start(self, session: str) -> None:
        def done(code, _results):
            self._starting = False
            if code != RESPONSE_SUCCESS:
                self._fail("the portal prompt was not accepted", code)
                return
            self._session = session
            log.info("pointer control granted; the phone can drive the mouse")
            self.daemon.notifications.show_local(
                summary="Trackpad ready",
                body="Your phone can now move the pointer.",
            )

        self._call_with_response("Start", (session,), done)

    def _fail(self, reason: str, code: int) -> None:
        self._starting = False
        if code == RESPONSE_CANCELLED:
            # Declined deliberately. Don't re-prompt on every swipe; a
            # reconnect clears this so they can change their mind.
            self._refused = True
            log.info("remote input declined by the user")
        else:
            log.warning("%s (portal response %d)", reason, code)

    def _close_session(self) -> None:
        for subscription in self._subscriptions:
            if self._bus is not None:
                self._bus.signal_unsubscribe(subscription)
        self._subscriptions.clear()

        if self._bus is None or self._session is None:
            self._session = None
            return

        try:
            self._bus.call_sync(
                PORTAL_BUS, self._session, SESSION_IFACE, "Close",
                None, None, Gio.DBusCallFlags.NONE, 2000, None,
            )
        except GLib.Error as exc:
            log.debug("closing the portal session failed: %s", exc.message)
        self._session = None


def _number(value: object) -> float | None:
    """Accept a JSON int or float; reject anything else, including bools."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # A hostile or buggy peer should not be able to fling the pointer to
    # infinity, and NaN would poison the compositor's state.
    if value != value or abs(value) > 10000:
        return None
    return float(value)
