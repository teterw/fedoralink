"""User settings, read from a small JSON file.

Deliberately stdlib-only: no gi, so the defaults and the merge logic are
unit-testable without PyGObject, and a broken config file can never stop
the daemon from starting.

Lives at ``$XDG_CONFIG_HOME/fedoralink/config.json`` (normally
``~/.config/fedoralink/config.json``). Absent is the normal case — every
key has a default, and the file only has to name what it overrides.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # Warn on the desktop when the phone's battery gets low.
    "battery_low_warning": True,
    "battery_low_threshold": 15,
    # Lock the desktop when the phone goes out of range. Off by default:
    # a flaky link that locks your screen mid-sentence is infuriating.
    "lock_on_disconnect": False,
    "lock_on_disconnect_grace_seconds": 30,
    # How long the desktop makes noise when the phone rings it.
    "pc_ring_seconds": 10,
}


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "fedoralink" / "config.json"


def merge(overrides: Any) -> dict[str, Any]:
    """Apply `overrides` onto the defaults, dropping anything unusable.

    A typo in one key must not take the rest of the file down with it, so
    each value is checked against the type of its default and skipped —
    loudly — rather than raising.
    """
    resolved = dict(DEFAULTS)

    if not isinstance(overrides, dict):
        log.warning("config is not a JSON object; using defaults")
        return resolved

    for key, value in overrides.items():
        if key not in DEFAULTS:
            log.warning("unknown config key %r; ignoring", key)
            continue

        default = DEFAULTS[key]
        # bool is a subclass of int, so check it first or True passes as 1.
        if isinstance(default, bool):
            if not isinstance(value, bool):
                log.warning("config key %r wants true/false; ignoring %r", key, value)
                continue
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                log.warning("config key %r wants a number; ignoring %r", key, value)
                continue
            if value < 0:
                log.warning("config key %r cannot be negative; ignoring %r", key, value)
                continue

        resolved[key] = value

    threshold = resolved["battery_low_threshold"]
    if not 0 <= threshold <= 100:
        log.warning("battery_low_threshold %r out of range; using default", threshold)
        resolved["battery_low_threshold"] = DEFAULTS["battery_low_threshold"]

    return resolved


def load(path: Path | None = None) -> dict[str, Any]:
    """Read the config file. Any failure falls back to the defaults."""
    path = path or config_path()

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return dict(DEFAULTS)
    except OSError as exc:
        log.warning("could not read %s: %s; using defaults", path, exc)
        return dict(DEFAULTS)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("%s is not valid JSON: %s; using defaults", path, exc)
        return dict(DEFAULTS)

    return merge(parsed)
