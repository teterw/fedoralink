"""Keep the phone's audio on the phone.

Bonding a phone to a PC makes the PC an audio sink, and Android will
happily route media there the moment the two are linked — so pressing play
on the phone comes out of the laptop, often to the user's surprise. That
routing is nothing FedoraLink asks for; it is what Android does with any
bonded device that advertises A2DP.

The phone cannot opt out of it: ``setConnectionPolicy`` is a system API,
so a normal app has no say in which profiles Android auto-connects. The
desktop can, though — BlueZ exposes ``DisconnectProfile`` per device, so
this plugin drops the audio profiles as they appear and leaves the rest of
the link alone.

Off by default, because the surprising behaviour is the one people want
stopped. Set ``connect_phone_audio`` to true to use the PC as a speaker.

AVRCP is deliberately left connected: it carries media *control*, not
audio, and disconnecting it would take the media keys with it.
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

from . import Plugin

log = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"

# Advanced Audio Distribution — the phone is the source, the PC the sink.
# Both are tried because which one BlueZ accepts depends on which end
# registered the profile.
A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"
A2DP_SINK = "0000110b-0000-1000-8000-00805f9b34fb"

# Hands-free and headset, so phone calls don't land on the PC either.
HANDSFREE = "0000111e-0000-1000-8000-00805f9b34fb"
HANDSFREE_AG = "0000111f-0000-1000-8000-00805f9b34fb"
HEADSET = "00001108-0000-1000-8000-00805f9b34fb"
HEADSET_AG = "00001112-0000-1000-8000-00805f9b34fb"

# Tried in turn; BlueZ rejects the ones a given phone doesn't advertise.
# On a Galaxy Z Flip6 the two that land are A2DP_SOURCE and HANDSFREE_AG,
# and dropping them leaves Device1.Connected true — so the RFCOMM link
# this daemon depends on is unaffected. Verified, not assumed.
AUDIO_UUIDS = (
    A2DP_SOURCE,
    A2DP_SINK,
    HANDSFREE,
    HANDSFREE_AG,
    HEADSET,
    HEADSET_AG,
)

# BlueZ publishes this under the device path when audio actually connects,
# which is the event worth reacting to. Watching for it beats a timer:
# Android may connect audio well after our RFCOMM link comes up.
MEDIA_TRANSPORT_IFACE = "org.bluez.MediaTransport1"


class AudioPlugin(Plugin):
    name = "audio"
    # Not packet-driven — this is entirely about the Bluetooth link.
    handles = ()

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        self._bus: Gio.DBusConnection | None = None
        self._added_sub: int | None = None
        self._device_path: str | None = None

    @property
    def _enabled(self) -> bool:
        """True when the user wants audio on the PC, so we stay out of it."""
        return bool(self.daemon.config["connect_phone_audio"])

    def start(self) -> None:
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as exc:
            log.warning("no system bus; cannot manage audio routing: %s", exc)
            return

        self._added_sub = self._bus.signal_subscribe(
            BLUEZ_BUS,
            "org.freedesktop.DBus.ObjectManager",
            "InterfacesAdded",
            "/",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_interfaces_added,
            None,
        )

    def stop(self) -> None:
        if self._bus is not None and self._added_sub is not None:
            self._bus.signal_unsubscribe(self._added_sub)
        self._added_sub = None
        self._bus = None
        self._device_path = None

    def on_connected(self, connection) -> None:
        self._device_path = connection.device_path
        # Audio may already be up — Android sometimes connects it first.
        if not self._enabled:
            self._drop_audio("link established")

    def on_disconnected(self, connection) -> None:
        self._device_path = None

    def _on_interfaces_added(
        self, _conn, _sender, _path, _iface, _signal, params, _user_data
    ) -> None:
        if self._enabled or self._device_path is None:
            return

        try:
            object_path, interfaces = params.unpack()
        except Exception:  # a malformed signal is not worth a traceback
            return

        if MEDIA_TRANSPORT_IFACE not in interfaces:
            return
        # Transports live beneath the device that owns them.
        if not object_path.startswith(self._device_path):
            return

        log.info("phone tried to route audio here; disconnecting it")
        self._drop_audio("audio transport appeared")

    def _drop_audio(self, why: str) -> None:
        if self._bus is None or self._device_path is None:
            return

        log.debug("dropping audio profiles (%s)", why)
        for uuid in AUDIO_UUIDS:
            # Async and fire-and-forget: most of these aren't connected, so
            # most of these calls are expected to fail.
            self._bus.call(
                BLUEZ_BUS,
                self._device_path,
                "org.bluez.Device1",
                "DisconnectProfile",
                GLib.Variant("(s)", (uuid,)),
                None,
                Gio.DBusCallFlags.NONE,
                10000,
                None,
                self._on_disconnect_done,
                uuid,
            )

    def _on_disconnect_done(
        self, bus: Gio.DBusConnection, result: Gio.AsyncResult, uuid: str
    ) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error as exc:
            # BlueZ answers "Invalid arguments" for a UUID the device never
            # advertised, which is most of this list for any given phone —
            # verified against a Galaxy Z Flip6, which offers 110a and 111f
            # but not 110b or 111e. Expected, so debug rather than warning.
            log.debug("DisconnectProfile(%s): %s", uuid[:8], exc.message)
        else:
            log.info("disconnected audio profile %s", uuid[:8])
