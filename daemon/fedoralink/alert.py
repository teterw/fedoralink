"""Make the desktop audible when the phone rings it.

The inverse of Find My Phone: a notification alone is useless for
locating a laptop under a cushion, so this actually makes noise.

``canberra-gtk-play`` is the sound player GNOME already depends on. It
plays a sound once and exits, so a repeating alert means re-spawning it on
a timer. When it isn't installed we fall back to the ``sound-name`` hint
on the notification itself, which the notification server honours — one
chime rather than a ring, but still audible.
"""

from __future__ import annotations

import logging
import shutil

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

# A freedesktop sound-theme name, so it works on any theme rather than
# pointing at a file that may not exist.
SOUND_NAME = "alarm-clock-elapsed"

# The sound is about a second long; leave a gap so it reads as a ring
# rather than a smear.
REPEAT_INTERVAL_MS = 1500


class Alerter:
    def __init__(self) -> None:
        self._source: int | None = None
        self._remaining = 0
        self._player = shutil.which("canberra-gtk-play")

        if self._player is None:
            log.info(
                "canberra-gtk-play not found; PC ring falls back to the "
                "notification sound hint"
            )

    @property
    def can_play(self) -> bool:
        return self._player is not None

    @property
    def fallback_sound_name(self) -> str | None:
        """Sound for the notification server to play, when we can't.

        None when canberra is available, so the chime doesn't double up
        with the ring.
        """
        return None if self.can_play else SOUND_NAME

    def start(self, seconds: int) -> None:
        """Ring for roughly `seconds`, restarting if already ringing."""
        self.stop()

        if self._player is None or seconds <= 0:
            return

        self._remaining = max(1, (seconds * 1000) // REPEAT_INTERVAL_MS)
        self._play_once()
        self._source = GLib.timeout_add(REPEAT_INTERVAL_MS, self._tick)

    def stop(self) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None
        self._remaining = 0

    def _tick(self) -> bool:
        self._remaining -= 1
        if self._remaining <= 0:
            self._source = None
            return GLib.SOURCE_REMOVE

        self._play_once()
        return GLib.SOURCE_CONTINUE

    def _play_once(self) -> None:
        if self._player is None:
            return
        try:
            # Fire and forget. Letting it inherit stdio would spam the
            # journal on every repeat, so both are discarded.
            Gio.Subprocess.new(
                [self._player, "-i", SOUND_NAME],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except GLib.Error as exc:
            log.debug("could not play alert sound: %s", exc)
            self.stop()
