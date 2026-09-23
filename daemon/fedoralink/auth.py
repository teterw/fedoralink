"""Shared-secret authentication for the link.

Bluetooth pairing decides which devices can *reach* the daemon. It says
nothing about which ones should be handed your clipboard and every
notification that crosses your screen. This module is the second gate.

Two phases:

**Enrollment**, once per device. The desktop mints a 32-byte secret and
sends it over the freshly bonded link, which Bluetooth has already
encrypted. Both ends display the same six-digit fingerprint of it, and the
desktop asks the human to approve. The digits are what catches a different
device having answered — they are for a person to compare, not a
cryptographic binding.

**Challenge-response**, every connection after. One side sends a random
nonce, the other returns ``HMAC-SHA256(secret, nonce)``. Nothing derived
from the secret crosses the link again, so a recorded session cannot be
replayed against a future one.

Stdlib only — no gi — so the crypto is unit-testable on its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# 256 bits, matching the HMAC-SHA256 block below. Anything shorter would
# be the weakest link in the chain.
SECRET_BYTES = 32
NONCE_BYTES = 32

FINGERPRINT_DIGITS = 6


def new_secret() -> str:
    """Mint a device secret, base64 for JSON transport and storage."""
    return base64.b64encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii")


def new_nonce() -> str:
    """Mint a single-use challenge."""
    return base64.b64encode(secrets.token_bytes(NONCE_BYTES)).decode("ascii")


def _decode(value: str, *, what: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{what} is not valid base64") from exc
    if not raw:
        raise ValueError(f"{what} is empty")
    return raw


def fingerprint(secret: str) -> str:
    """Six digits for a human to compare across two screens.

    Deliberately a hash of the secret rather than a slice of it: showing
    part of the key itself on a lock screen, where notifications land,
    would leak it to anyone glancing at the phone.
    """
    digest = hashlib.sha256(_decode(secret, what="secret")).digest()
    value = int.from_bytes(digest[:8], "big") % (10**FINGERPRINT_DIGITS)
    return f"{value:0{FINGERPRINT_DIGITS}d}"


def respond(secret: str, nonce: str) -> str:
    """Answer a challenge."""
    mac = hmac.new(
        _decode(secret, what="secret"),
        _decode(nonce, what="nonce"),
        hashlib.sha256,
    )
    return mac.hexdigest()


def verify(secret: str, nonce: str, response: object) -> bool:
    """Check a challenge response.

    Never raises: a malformed or missing response from a hostile peer is
    an authentication failure, not a crash. Comparison is constant-time so
    the daemon doesn't leak the expected MAC one byte at a time.
    """
    if not isinstance(response, str) or not response:
        return False

    try:
        expected = respond(secret, nonce)
    except ValueError:
        return False

    return hmac.compare_digest(expected, response)
