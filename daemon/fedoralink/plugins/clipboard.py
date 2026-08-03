"""Two-way clipboard sync.

Wayland has no clipboard-change signal in the core protocol. ``wl-paste
--watch`` depends on the ``data-control`` protocol, and Mutter's support
for it has historically lagged behind other compositors — so instead of
betting the feature on it, we poll ``wl-paste`` on a timer.

Polling only runs while a phone is actually connected, so the idle cost
is zero, and it works on every compositor without a capability check.
"""

from __future__ import annotations

import logging
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import Plugin
from ..protocol import CLIPBOARD

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2

# Well past any sane copy-paste, and far past what RFCOMM should carry.
MAX_CLIPBOARD_BYTES = 64 * 1024


class ClipboardPlugin(Plugin):
    name = "clipboard"
    handles = (CLIPBOARD,)

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        self._poll_source: int | None = None
        # Last content we know both sides agree on. Guards against echoing
        # a value straight back to whoever just sent it.
        self._last_seen: str | None = None
        self._poll_in_flight = False

    def on_connected(self, connection) -> None:
        if self._poll_source is None:
            self._poll_source = GLib.timeout_add_seconds(
                POLL_INTERVAL_SECONDS, self._poll
            )

    def on_disconnected(self, connection) -> None:
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
            self._poll_source = None
        self._last_seen = None

    def stop(self) -> None:
        self.on_disconnected(None)

    def on_packet(self, packet: dict[str, Any]) -> None:
        content = packet["body"].get("content")
        if not isinstance(content, str) or not content:
            return
        if content == self._last_seen:
            return

        self._last_seen = content
        self._set_local_clipboard(content)

    def send_current(self) -> bool:
        """Push the desktop clipboard to the phone right now.

        Wired to the Quick Settings menu so the feature still works even
        if polling is disabled or the compositor misbehaves.
        """
        self._poll(force=True)
        return True

    def _poll(self, force: bool = False) -> bool:
        # wl-paste is fast, but overlapping spawns on a slow system would
        # stack up. One in flight at a time.
        if self._poll_in_flight:
            return True
        self._poll_in_flight = True

        try:
            proc = Gio.Subprocess.new(
                ["wl-paste", "--no-newline", "--type", "text/plain"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
        except GLib.Error as exc:
            log.warning("wl-paste unavailable, clipboard sync off: %s", exc)
            self._poll_in_flight = False
            self._poll_source = None
            return False

        proc.communicate_utf8_async(None, None, self._on_paste_done, force)
        return True

    def _on_paste_done(
        self, proc: Gio.Subprocess, result: Gio.AsyncResult, force: bool
    ) -> None:
        self._poll_in_flight = False
        try:
            ok, stdout, _ = proc.communicate_utf8_finish(result)
        except GLib.Error as exc:
            log.debug("wl-paste failed: %s", exc)
            return

        # Exit code 1 is the normal "nothing is copied" case, and a
        # non-text clipboard (an image) also lands here. Both are fine.
        if not ok or not proc.get_successful() or not stdout:
            return

        if len(stdout.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            log.debug("clipboard too large to sync (%d chars)", len(stdout))
            return

        if not force and stdout == self._last_seen:
            return
        if force and stdout == self._last_seen:
            # Explicit user request — resend even though nothing changed.
            pass

        self._last_seen = stdout
        self.send(CLIPBOARD, {"content": stdout})

    def _set_local_clipboard(self, content: str) -> None:
        try:
            proc = Gio.Subprocess.new(
                ["wl-copy", "--type", "text/plain"],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDERR_SILENCE,
            )
            proc.communicate_utf8_async(content, None, None, None)
        except GLib.Error as exc:
            log.warning("wl-copy failed: %s", exc)
