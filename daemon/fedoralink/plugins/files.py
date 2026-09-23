"""Send and receive files.

Two jobs, both deliberately unexciting: ask the user before writing
anything to their disk, and never leave a half-file behind. The accounting
lives in ``transfers.py`` and is tested there; this is the I/O, the
notifications and the back-pressure.

**Back-pressure matters here more than anywhere else in the project.**
RFCOMM drains at roughly 200 KB/s. A pump that just fed chunks to the
socket as fast as it could read them would queue an entire file in memory
in a second or two, so it waits whenever the link is already backed up.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
from pathlib import Path

from gi.repository import GLib

from ..protocol import (
    FILE_ACCEPT,
    FILE_CANCEL,
    FILE_CHUNK,
    FILE_DONE,
    FILE_OFFER,
)
from ..transfers import (
    IncomingTransfer,
    OutgoingTransfer,
    TransferError,
    unique_path,
    validate_offer,
)
from . import Plugin

log = logging.getLogger(__name__)

ACCEPT_ACTION = "fedoralink-accept-file"
REJECT_ACTION = "fedoralink-reject-file"

# Stop feeding the socket above this and let it drain. Two chunks' worth,
# so there is always something queued and never much.
HIGH_WATER_BYTES = 96 * 1024

# How often to retry when the link is backed up.
DRAIN_INTERVAL_MS = 50

# Bluetooth's real throughput, for the "this will take a while" estimate.
RFCOMM_BYTES_PER_SECOND = 200 * 1024


def human_size(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GB"


def human_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


class FilesPlugin(Plugin):
    name = "files"
    handles = (FILE_OFFER, FILE_ACCEPT, FILE_CHUNK, FILE_DONE, FILE_CANCEL)

    def __init__(self, daemon) -> None:
        super().__init__(daemon)
        self._incoming: IncomingTransfer | None = None
        self._outgoing: OutgoingTransfer | None = None
        self._pump_source: int | None = None
        self._progress_id: int | None = None
        self._prompt_id: int | None = None
        self._pending_offer: tuple[str, str, int] | None = None

    # ------------------------------------------------------------ lifecycle

    def on_disconnected(self, connection) -> None:
        # A dropped link mid-transfer: fail cleanly rather than leaving a
        # partial file wearing the real name.
        if self._incoming is not None:
            log.info("link dropped during an incoming transfer; discarding")
            self._incoming.discard()
            self._notify("Transfer failed", "The phone disconnected.")
        if self._outgoing is not None:
            log.info("link dropped during an outgoing transfer")
            self._outgoing.close()
        self._reset()

    def _reset(self) -> None:
        self._stop_pump()
        self.daemon.notifications.cancel_ask(self._prompt_id)
        self._prompt_id = None
        self._pending_offer = None
        self._incoming = None
        self._outgoing = None
        if self._progress_id is not None:
            self._progress_id = None

    # -------------------------------------------------------------- inbound

    def on_packet(self, packet) -> None:
        packet_type = packet["type"]
        body = packet["body"]

        if packet_type == FILE_OFFER:
            self._on_offer(body)
        elif packet_type == FILE_CHUNK:
            self._on_chunk(body)
        elif packet_type == FILE_DONE:
            self._on_done(body)
        elif packet_type == FILE_CANCEL:
            self._on_remote_cancel(body)
        elif packet_type == FILE_ACCEPT:
            self._on_accept(body)

    def _on_offer(self, body) -> None:
        if self._incoming is not None or self._pending_offer is not None:
            self._cancel_remote(body.get("id"), "already receiving a file")
            return

        try:
            transfer_id, name, size = validate_offer(body)
        except TransferError as exc:
            log.warning("refusing offer: %s", exc)
            self._cancel_remote(body.get("id"), str(exc))
            return

        self._pending_offer = (transfer_id, name, size)

        detail = human_size(size)
        if not self.daemon.on_lan:
            # Honesty about Bluetooth: an 80 MB video is seven minutes, and
            # finding that out afterwards is worse than being told.
            estimate = human_duration(max(1, size) / RFCOMM_BYTES_PER_SECOND)
            detail = f"{detail} · about {estimate} over Bluetooth"

        who = self.daemon.device_name or "Your phone"
        self._prompt_id = self.daemon.notifications.ask(
            summary=f"Receive {name}?",
            body=f"{who} wants to send it — {detail}",
            actions=[(ACCEPT_ACTION, "Save"), (REJECT_ACTION, "Decline")],
            on_action=self._offer_answered,
        )

        if self._prompt_id is None:
            self._cancel_remote(transfer_id, "could not ask the user")
            self._pending_offer = None

    def _offer_answered(self, action) -> None:
        self._prompt_id = None
        offer = self._pending_offer
        self._pending_offer = None

        if offer is None:
            return

        transfer_id, name, size = offer
        if action != ACCEPT_ACTION:
            self._cancel_remote(transfer_id, "declined")
            return

        directory = self._download_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            destination = unique_path(directory, name)
            self._incoming = IncomingTransfer(transfer_id, destination, size)
        except (OSError, TransferError) as exc:
            log.error("cannot receive %s: %s", name, exc)
            self._cancel_remote(transfer_id, str(exc))
            self._notify("Cannot receive the file", str(exc))
            return

        self.send(FILE_ACCEPT, {"id": transfer_id})
        self._show_progress(f"Receiving {name}", 0.0)

    def _on_chunk(self, body) -> None:
        transfer = self._incoming
        if transfer is None or body.get("id") != transfer.id:
            return

        try:
            data = base64.b64decode(body.get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            self._fail_incoming(f"chunk was not valid base64: {exc}")
            return

        try:
            transfer.write_chunk(body.get("seq"), data)
        except TransferError as exc:
            self._fail_incoming(str(exc))
            return

        self._show_progress(f"Receiving {transfer.destination.name}", transfer.progress)

    def _on_done(self, body) -> None:
        transfer = self._incoming
        if transfer is None or body.get("id") != transfer.id:
            return

        self._incoming = None
        try:
            path = transfer.finish(body.get("sha256"))
        except TransferError as exc:
            log.error("incoming transfer failed: %s", exc)
            self._notify("Transfer failed", str(exc))
            return

        log.info("received %s", path)
        self._notify("File received", str(path))

    def _on_remote_cancel(self, body) -> None:
        reason = body.get("reason") or "the phone cancelled it"

        if self._incoming is not None and body.get("id") == self._incoming.id:
            self._incoming.discard()
            self._incoming = None
            self._notify("Transfer cancelled", str(reason))
            return

        if self._outgoing is not None and body.get("id") == self._outgoing.id:
            log.info("phone cancelled the outgoing transfer: %s", reason)
            self._outgoing.close()
            self._outgoing = None
            self._stop_pump()
            self._notify("Send cancelled", str(reason))

    def _fail_incoming(self, reason: str) -> None:
        log.warning("incoming transfer failed: %s", reason)
        if self._incoming is not None:
            self._cancel_remote(self._incoming.id, reason)
            self._incoming.discard()
            self._incoming = None
        self._notify("Transfer failed", reason)

    # ------------------------------------------------------------- outbound

    def send_file(self, path_text: str) -> bool:
        """Offer a local file to the phone."""
        if not self.daemon.authenticated:
            log.warning("cannot send a file: no authenticated phone")
            return False
        if self._outgoing is not None:
            self._notify("Already sending", "One file at a time.")
            return False

        path = Path(path_text).expanduser()
        if not path.is_file():
            log.warning("cannot send %s: not a file", path)
            self._notify("Cannot send", f"{path} is not a file.")
            return False

        try:
            transfer = OutgoingTransfer(secrets.token_hex(8), path)
        except (OSError, TransferError) as exc:
            log.error("cannot send %s: %s", path, exc)
            self._notify("Cannot send", str(exc))
            return False

        self._outgoing = transfer
        self.send(
            FILE_OFFER,
            {"id": transfer.id, "name": transfer.name, "size": transfer.size},
        )
        log.info("offered %s (%d bytes)", transfer.name, transfer.size)
        return True

    def _on_accept(self, body) -> None:
        transfer = self._outgoing
        if transfer is None or body.get("id") != transfer.id:
            return

        log.info("phone accepted %s; sending", transfer.name)
        self._show_progress(f"Sending {transfer.name}", 0.0)
        self._start_pump()

    def _start_pump(self) -> None:
        self._stop_pump()
        self._pump_source = GLib.idle_add(self._pump)

    def _stop_pump(self) -> None:
        if self._pump_source is not None:
            GLib.source_remove(self._pump_source)
            self._pump_source = None

    def _pump(self) -> bool:
        transfer = self._outgoing
        if transfer is None:
            self._pump_source = None
            return GLib.SOURCE_REMOVE

        # Let the socket drain before queueing more. Without this an 80 MB
        # file becomes 80 MB of outbox in about a second.
        if self.daemon.pending_bytes > HIGH_WATER_BYTES:
            self._pump_source = GLib.timeout_add(DRAIN_INTERVAL_MS, self._pump)
            return GLib.SOURCE_REMOVE

        chunk = transfer.next_chunk()
        if chunk is None:
            self._pump_source = None
            self.send(FILE_DONE, {"id": transfer.id, "sha256": transfer.sha256()})
            log.info("sent %s", transfer.name)
            self._notify("File sent", transfer.name)
            transfer.close()
            self._outgoing = None
            return GLib.SOURCE_REMOVE

        seq, data = chunk
        self.send(
            FILE_CHUNK,
            {
                "id": transfer.id,
                "seq": seq,
                "data": base64.b64encode(data).decode("ascii"),
            },
        )
        self._show_progress(f"Sending {transfer.name}", transfer.progress)
        return GLib.SOURCE_CONTINUE

    def cancel(self) -> bool:
        """Cancel whatever is in flight, from the desktop side."""
        cancelled = False

        if self._incoming is not None:
            self._cancel_remote(self._incoming.id, "cancelled on the PC")
            self._incoming.discard()
            self._incoming = None
            cancelled = True

        if self._outgoing is not None:
            self._cancel_remote(self._outgoing.id, "cancelled on the PC")
            self._outgoing.close()
            self._outgoing = None
            self._stop_pump()
            cancelled = True

        if cancelled:
            self._notify("Transfer cancelled", "")
        return cancelled

    # --------------------------------------------------------------- helpers

    def _cancel_remote(self, transfer_id, reason: str) -> None:
        if isinstance(transfer_id, str) and transfer_id:
            self.send(FILE_CANCEL, {"id": transfer_id, "reason": reason})

    @staticmethod
    def _download_dir() -> Path:
        # XDG's answer if the user has one, otherwise the conventional path.
        try:
            special = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        except Exception:
            special = None
        if special:
            return Path(special)
        return Path(os.path.expanduser("~/Downloads"))

    def _show_progress(self, summary: str, fraction: float) -> None:
        percent = int(fraction * 100)
        # Replace in place rather than stacking one notification per chunk.
        self._progress_id = self.daemon.notifications.show_progress(
            self._progress_id, summary, f"{percent}%"
        )

    def _notify(self, summary: str, body: str) -> None:
        self.daemon.notifications.show_local(summary=summary, body=body)
