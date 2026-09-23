"""The trust store: which devices have been approved, and their secrets.

Lives at ``$XDG_DATA_HOME/fedoralink/devices.json``, created 0600. It holds
key material, so the permissions are enforced on every write rather than
left to whatever umask the daemon inherited.

Stdlib only — no gi — so it is unit-testable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Owner read/write only. Anything wider and another local account could
# read the secret and impersonate the phone.
FILE_MODE = 0o600
DIR_MODE = 0o700


def store_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "fedoralink" / "devices.json"


class DeviceStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or store_path()
        self._devices: dict[str, dict[str, Any]] = {}
        self.load()

    # ------------------------------------------------------------- disk io

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._devices = {}
            return
        except OSError as exc:
            # Unreadable store means no device can authenticate. Refusing
            # every peer is the safe failure here, so don't pretend it's
            # empty and silently re-enroll.
            log.error("could not read %s: %s", self.path, exc)
            self._devices = {}
            return

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.error("%s is not valid JSON: %s", self.path, exc)
            self._devices = {}
            return

        if not isinstance(parsed, dict):
            log.error("%s is not a JSON object", self.path)
            self._devices = {}
            return

        self._devices = {
            key: value
            for key, value in parsed.items()
            if isinstance(value, dict) and isinstance(value.get("secret"), str)
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)

        # Write-then-rename, so a crash mid-write can't leave a truncated
        # store that locks out every device. The temp file is created in
        # the same directory so the rename is atomic.
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".devices-", suffix=".json"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._devices, handle, indent=2, sort_keys=True)
                handle.write("\n")
            # mkstemp is already 0600, but the store may pre-date this code
            # or have been copied in by hand.
            os.chmod(tmp, FILE_MODE)
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    # -------------------------------------------------------------- lookup

    def secret_for(self, device_id: str) -> str | None:
        entry = self._devices.get(device_id)
        return entry["secret"] if entry else None

    def is_trusted(self, device_id: str) -> bool:
        return self.secret_for(device_id) is not None

    def name_for(self, device_id: str) -> str | None:
        entry = self._devices.get(device_id)
        return entry.get("name") if entry else None

    def known_devices(self) -> dict[str, str]:
        """device id -> display name, for listing and revoking."""
        return {
            key: value.get("name") or key for key, value in self._devices.items()
        }

    # ------------------------------------------------------------- mutation

    def trust(self, device_id: str, name: str, secret: str) -> None:
        self._devices[device_id] = {"name": name, "secret": secret}
        self.save()
        log.info("trusted device %s (%s)", name, device_id)

    def revoke(self, device_id: str) -> bool:
        if self._devices.pop(device_id, None) is None:
            return False
        self.save()
        log.info("revoked device %s", device_id)
        return True
