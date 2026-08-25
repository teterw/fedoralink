"""Two-way clipboard sync.

Wayland has no clipboard-change signal in the core protocol, and Mutter
still does not implement ``data-control`` (rechecked on Mutter 50), so
``wl-paste --watch`` refuses to run. The obvious fallback — polling
``wl-paste`` on a timer — turned out to be worse than it looked. Without
``data-control`` a ``wl-paste`` run has no way to read the selection except
to become a focusable client, so it maps a real ``xdg_toplevel``:

    xdg_surface.get_toplevel(new id xdg_toplevel)
    xdg_toplevel.set_app_id("io.github.bugaevc.wl-clipboard")

At one spawn every two seconds that put a window in the dock and took it
away again, forever, which made docks resize on a loop.

So the read side lives in the GNOME Shell extension now. The shell *is* the
compositor: it reads and writes the selection with no client, no window and
no focus requirement, and Mutter gives it a real ``owner-changed`` signal —
which makes the sync event-driven instead of polled, so a copy reaches the
phone immediately rather than up to two seconds later.

``wl-copy`` survives only as the write-side fallback for when the extension
isn't running (disabled, or the shell is mid-reload).
"""

from __future__ import annotations

import logging
from typing import Any

from gi.repository import Gio, GLib

from ..protocol import CLIPBOARD
from . import Plugin

log = logging.getLogger(__name__)

# Well past any sane copy-paste, and far past what RFCOMM should carry.
MAX_CLIPBOARD_BYTES = 64 * 1024


class ClipboardPlugin(Plugin):
    name = "clipboard"
    handles = (CLIPBOARD,)

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        # Last content we know both sides agree on. Guards against echoing
        # a value straight back to whoever just sent it.
        self._last_seen: str | None = None

    def on_disconnected(self, connection) -> None:
        self._last_seen = None

    # ------------------------------------------------------ from the phone

    def on_packet(self, packet: dict[str, Any]) -> None:
        content = packet["body"].get("content")
        if not isinstance(content, str) or not content:
            return
        if content == self._last_seen:
            return

        self._last_seen = content
        self._set_local_clipboard(content)

    # ------------------------------------------------- from the shell side

    def set_from_shell(self, content: str, force: bool = False) -> None:
        """The desktop clipboard changed; the extension read it for us.

        Called on every local copy, so the dedupe here is what stops a
        phone-originated value from being bounced straight back.
        """
        if not content:
            return
        if not force and content == self._last_seen:
            return
        if len(content.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            log.debug("clipboard too large to sync (%d chars)", len(content))
            return

        self._last_seen = content
        self.send(CLIPBOARD, {"content": content})

    def send_current(self) -> bool:
        """Resend whatever we last saw.

        The extension normally drives sending directly; this is the path
        for a Quick Settings tap made while the bridge is unavailable.
        """
        if self._last_seen:
            self.send(CLIPBOARD, {"content": self._last_seen})
            return True
        return False

    # ----------------------------------------------------- writing locally

    def _set_local_clipboard(self, content: str) -> None:
        # Preferred: hand it to the shell, which owns the selection anyway
        # and needs no process to do it.
        if self.daemon.dbus.clipboard_bridge_active:
            self.daemon.dbus.emit_clipboard(content)
            return

        # Fallback only. wl-copy maps a toplevel exactly like wl-paste, but
        # this runs on a real clipboard change rather than on a timer.
        try:
            proc = Gio.Subprocess.new(
                ["wl-copy", "--type", "text/plain"],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
            proc.communicate_utf8_async(content, None, None, None)
        except GLib.Error as exc:
            log.warning("wl-copy failed: %s", exc)
