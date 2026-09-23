"""File transfer accounting: the part where a mistake corrupts a file.

Kept separate from the I/O and from gi so every rule here is under test.
The rules that matter:

* A transfer writes to a temporary file and is renamed into place only
  after the hash matches. A dropped link therefore leaves no half-file
  wearing the real name.
* Chunks must arrive in order. Out-of-order or duplicate chunks are a
  protocol error, not something to paper over — accepting them silently is
  how you get a file that is the right length and the wrong contents.
* The declared size is a hard limit. A peer that keeps sending past it is
  cut off rather than allowed to fill the disk.
* Names from the peer are never trusted as paths.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Generous, but not unbounded: a peer shouldn't be able to announce a
# petabyte and have us reserve anything for it.
MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024

# 32 KiB raw is ~44 KiB of base64, comfortably inside MAX_LINE_BYTES while
# keeping the per-packet overhead small.
CHUNK_BYTES = 32 * 1024

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class TransferError(Exception):
    """The transfer cannot continue. The caller must cancel and clean up."""


def safe_name(name: object, fallback: str = "received-file") -> str:
    """Reduce a peer-supplied name to something safe to create.

    Never trusted as a path: directory separators, traversal and leading
    dots all go, because the peer chose this string and it ends up on our
    filesystem.
    """
    if not isinstance(name, str):
        return fallback

    # Take the last component, so "../../etc/passwd" becomes "passwd".
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE.sub("_", base).lstrip(".")

    # Leave room for the " (2)" that unique_path may add.
    base = base[:200].strip("_")
    return base or fallback


def unique_path(directory: Path, name: str) -> Path:
    """A path in `directory` that doesn't exist yet.

    Silently overwriting a file the user already had is not an acceptable
    outcome of accepting a transfer.
    """
    candidate = directory / name
    if not candidate.exists():
        return candidate

    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 1000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate

    raise TransferError(f"could not find a free name for {name!r}")


def validate_offer(body: dict) -> tuple[str, str, int]:
    """Check an incoming offer. Returns (transfer_id, name, size)."""
    transfer_id = body.get("id")
    if not isinstance(transfer_id, str) or not transfer_id:
        raise TransferError("offer has no id")

    size = body.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise TransferError("offer has no usable size")
    if size > MAX_FILE_BYTES:
        raise TransferError(f"offer of {size} bytes is too large")

    return transfer_id, safe_name(body.get("name")), size


class IncomingTransfer:
    """Accumulates chunks into a temp file, then renames on a hash match."""

    def __init__(self, transfer_id: str, destination: Path, size: int) -> None:
        self.id = transfer_id
        self.destination = destination
        self.size = size
        self.received = 0
        self.next_seq = 0

        self._digest = hashlib.sha256()
        # Same directory as the destination, so the final rename is atomic
        # rather than a copy across filesystems.
        self.temp_path = destination.with_name(f".{destination.name}.part")
        self._handle = self.temp_path.open("wb")
        self._closed = False

    @property
    def progress(self) -> float:
        if self.size == 0:
            return 1.0
        return min(1.0, self.received / self.size)

    def write_chunk(self, seq: object, data: bytes) -> None:
        if self._closed:
            raise TransferError("chunk for a finished transfer")

        if not isinstance(seq, int) or isinstance(seq, bool):
            raise TransferError("chunk has no sequence number")
        if seq != self.next_seq:
            # Silently accepting this is how you get a file of the right
            # length and the wrong contents.
            raise TransferError(f"chunk out of order: got {seq}, want {self.next_seq}")

        if self.received + len(data) > self.size:
            raise TransferError("peer sent more data than it declared")

        self._handle.write(data)
        self._digest.update(data)
        self.received += len(data)
        self.next_seq += 1

    def finish(self, expected_hash: object) -> Path:
        """Verify and move into place. Returns the final path."""
        if self._closed:
            raise TransferError("transfer already finished")

        self._close_handle()

        if self.received != self.size:
            self.discard()
            raise TransferError(
                f"transfer ended early: {self.received} of {self.size} bytes"
            )

        actual = self._digest.hexdigest()
        if isinstance(expected_hash, str) and expected_hash:
            if actual != expected_hash.lower():
                self.discard()
                raise TransferError("file hash does not match")

        try:
            self.temp_path.replace(self.destination)
        except OSError as exc:
            self.discard()
            raise TransferError(f"could not save the file: {exc}") from exc

        return self.destination

    def discard(self) -> None:
        """Abandon the transfer, leaving nothing behind."""
        self._close_handle()
        self.temp_path.unlink(missing_ok=True)

    def _close_handle(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.close()
        except OSError:
            pass


class OutgoingTransfer:
    """Reads a local file in chunks, hashing as it goes."""

    def __init__(self, transfer_id: str, source: Path) -> None:
        self.id = transfer_id
        self.source = source

        stat = source.stat()
        if stat.st_size > MAX_FILE_BYTES:
            raise TransferError(f"{source.name} is too large to send")

        self.size = stat.st_size
        self.sent = 0
        self.next_seq = 0
        self._digest = hashlib.sha256()
        self._handle = source.open("rb")
        self._closed = False

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def progress(self) -> float:
        if self.size == 0:
            return 1.0
        return min(1.0, self.sent / self.size)

    @property
    def done(self) -> bool:
        return self.sent >= self.size

    def next_chunk(self) -> tuple[int, bytes] | None:
        """Next (seq, data), or None when there is nothing left."""
        if self._closed:
            return None

        data = self._handle.read(CHUNK_BYTES)
        if not data:
            return None

        seq = self.next_seq
        self.next_seq += 1
        self.sent += len(data)
        self._digest.update(data)
        return seq, data

    def sha256(self) -> str:
        return self._digest.hexdigest()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.close()
        except OSError:
            pass
