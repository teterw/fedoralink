"""Lock the desktop when the phone leaves range.

Off by default. A Bluetooth link drops for all sorts of uninteresting
reasons — the phone's service restarting, a microwave, walking past a
doorway — and a screen that locks every time one happens is worse than no
feature at all. Hence the grace period: the phone has to stay gone.
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

from . import Plugin

log = logging.getLogger(__name__)

SCREENSAVER_BUS = "org.gnome.ScreenSaver"
SCREENSAVER_PATH = "/org/gnome/ScreenSaver"
SCREENSAVER_IFACE = "org.gnome.ScreenSaver"


class PresencePlugin(Plugin):
    name = "presence"
    # Not packet-driven: it works purely off connect/disconnect.
    handles = ()

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        self._bus: Gio.DBusConnection | None = None
        self._grace_source: int | None = None

    def start(self) -> None:
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def stop(self) -> None:
        self._cancel_grace()
        self._bus = None

    def on_connected(self, connection) -> None:
        # Back in range inside the grace period: nothing to do.
        self._cancel_grace()

    def on_disconnected(self, connection) -> None:
        if not self.daemon.config["lock_on_disconnect"]:
            return

        grace = self.daemon.config["lock_on_disconnect_grace_seconds"]
        self._cancel_grace()

        if grace <= 0:
            self._lock()
            return

        log.info("phone left; locking in %ds unless it comes back", grace)
        self._grace_source = GLib.timeout_add_seconds(grace, self._grace_elapsed)

    def _grace_elapsed(self) -> bool:
        self._grace_source = None

        # Belt and braces: on_connected cancels the timer, but if the order
        # ever changed, locking a desktop whose phone is present would be a
        # nasty surprise.
        if self.daemon.connected:
            log.debug("phone returned before the grace period elapsed")
            return GLib.SOURCE_REMOVE

        self._lock()
        return GLib.SOURCE_REMOVE

    def _cancel_grace(self) -> None:
        if self._grace_source is not None:
            GLib.source_remove(self._grace_source)
            self._grace_source = None

    def _lock(self) -> None:
        if self._bus is None:
            return

        log.info("locking the session: phone out of range")
        try:
            self._bus.call_sync(
                SCREENSAVER_BUS, SCREENSAVER_PATH, SCREENSAVER_IFACE, "Lock",
                None, None, Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            # Not GNOME, or the screensaver isn't running. Nothing to do
            # but say so — failing to lock must not take the daemon down.
            log.warning("could not lock the session: %s", exc.message)
