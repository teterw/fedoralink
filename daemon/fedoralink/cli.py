"""Command-line client for the running daemon.

Everything here talks to the daemon over the session bus rather than doing
the work itself — there is only one Bluetooth link and the daemon owns it.
That also means these commands work from a Nautilus script, a keyboard
shortcut or a terminal without caring which.
"""

from __future__ import annotations

import sys
from pathlib import Path

from gi.repository import Gio, GLib

from .dbus_service import BUS_NAME, INTERFACE, OBJECT_PATH


class DaemonUnavailable(Exception):
    """The daemon isn't running, or is too old to understand the request."""


def _proxy() -> Gio.DBusProxy:
    try:
        return Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            # Don't auto-start: if the daemon isn't running, say so rather
            # than launching one that has no phone attached.
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            None,
        )
    except GLib.Error as exc:
        raise DaemonUnavailable(f"could not reach the daemon: {exc.message}") from exc


def _call(method: str, args: GLib.Variant | None = None) -> GLib.Variant:
    proxy = _proxy()
    if proxy.get_name_owner() is None:
        raise DaemonUnavailable(
            "the daemon isn't running — try: systemctl --user start fedoralink"
        )

    try:
        return proxy.call_sync(method, args, Gio.DBusCallFlags.NONE, 30000, None)
    except GLib.Error as exc:
        if "UnknownMethod" in exc.message or "no such method" in exc.message.lower():
            raise DaemonUnavailable(
                f"the running daemon has no {method} — it predates this feature. "
                "Reinstall with ./install.sh, then: systemctl --user restart fedoralink"
            ) from exc
        raise DaemonUnavailable(exc.message) from exc


# ------------------------------------------------------------------ commands


def send(paths: list[str]) -> int:
    """Offer files to the phone, one at a time."""
    failures = 0

    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            print(f"not a file: {path}", file=sys.stderr)
            failures += 1
            continue

        result = _call("SendFile", GLib.Variant("(s)", (str(path),)))
        if result.unpack()[0]:
            print(f"offered {path.name}")
        else:
            # The daemon logs why; repeating its reasoning here would drift.
            print(f"daemon refused {path.name} — see journalctl --user -u fedoralink",
                  file=sys.stderr)
            failures += 1

    return 1 if failures else 0


def cancel() -> int:
    if _call("CancelTransfer").unpack()[0]:
        print("transfer cancelled")
        return 0
    print("nothing in flight")
    return 0


def devices() -> int:
    known = _call("ListDevices").unpack()[0]
    if not known:
        print("no enrolled devices")
        return 0
    for device_id, name in sorted(known.items(), key=lambda item: item[1]):
        print(f"{name}\t{device_id}")
    return 0


def forget() -> int:
    count = _call("ForgetDevices").unpack()[0]
    print(f"forgot {count} device(s); the next connection will re-enroll")
    return 0


def ping() -> int:
    _call("Ping")
    print("ringing the phone")
    return 0


def status() -> int:
    proxy = _proxy()
    if proxy.get_name_owner() is None:
        print("daemon: not running")
        return 1

    def prop(name: str, default: object = None) -> object:
        value = proxy.get_cached_property(name)
        return value.unpack() if value is not None else default

    # An absent property means the running daemon predates the feature.
    # Reporting that as "no" would be a lie with the same shape as the
    # truth, which is the worst kind.
    missing = object()

    def flag(name: str, yes: str = "yes", no: str = "no") -> str:
        value = prop(name, missing)
        if value is missing:
            return "unknown (daemon predates this)"
        return yes if value else no

    connected = prop("Connected", False)
    print("daemon:        running")
    print(f"phone:         {prop('DeviceName') or '(none)'}")
    print(f"connected:     {'yes' if connected else 'no'}")
    print(f"authenticated: {flag('Authenticated')}")
    print(f"transport:     {flag('OnLan', yes='LAN', no='Bluetooth')}")

    level = prop("BatteryLevel", -1)
    if isinstance(level, int) and level >= 0:
        charging = " (charging)" if prop("BatteryCharging", False) else ""
        print(f"battery:       {level}%{charging}")

    if prop("MediaHasSession", False):
        title = prop("MediaTitle") or "unknown"
        artist = prop("MediaArtist")
        playing = "playing" if prop("MediaPlaying", False) else "paused"
        label = f"{title} — {artist}" if artist else title
        print(f"media:         {label} ({playing})")

    return 0 if connected else 1
