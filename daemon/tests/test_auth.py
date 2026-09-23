"""Challenge-response and fingerprint behaviour."""

from __future__ import annotations

import base64

import pytest

from fedoralink.auth import (
    FINGERPRINT_DIGITS,
    SECRET_BYTES,
    fingerprint,
    new_nonce,
    new_secret,
    respond,
    verify,
)


class TestSecrets:
    def test_secret_is_256_bits(self):
        assert len(base64.b64decode(new_secret())) == SECRET_BYTES

    def test_secrets_are_unique(self):
        assert len({new_secret() for _ in range(50)}) == 50

    def test_nonces_are_unique(self):
        assert len({new_nonce() for _ in range(50)}) == 50


class TestFingerprint:
    def test_is_six_digits(self):
        fp = fingerprint(new_secret())
        assert len(fp) == FINGERPRINT_DIGITS
        assert fp.isdigit()

    def test_is_stable_for_the_same_secret(self):
        secret = new_secret()
        assert fingerprint(secret) == fingerprint(secret)

    def test_differs_between_secrets(self):
        prints = {fingerprint(new_secret()) for _ in range(200)}
        # Six digits over 200 samples: a couple of collisions would be
        # unremarkable, near-total collapse would mean a broken derivation.
        assert len(prints) > 190

    def test_keeps_leading_zeros(self):
        # "001234" must not render as "1234" — the user is comparing digit
        # strings across two screens.
        assert all(len(fingerprint(new_secret())) == 6 for _ in range(50))

    def test_is_not_a_slice_of_the_secret(self):
        # The fingerprint shows up on a lock screen; leaking key bytes
        # there would hand the secret to anyone glancing at the phone.
        secret = new_secret()
        raw = base64.b64decode(secret).hex()
        assert fingerprint(secret) not in raw

    def test_rejects_a_non_base64_secret(self):
        with pytest.raises(ValueError, match="not valid base64"):
            fingerprint("!!! not base64 !!!")

    def test_rejects_an_empty_secret(self):
        with pytest.raises(ValueError, match="empty"):
            fingerprint("")


class TestChallengeResponse:
    def test_correct_response_verifies(self):
        secret, nonce = new_secret(), new_nonce()
        assert verify(secret, nonce, respond(secret, nonce)) is True

    def test_wrong_secret_fails(self):
        nonce = new_nonce()
        assert verify(new_secret(), nonce, respond(new_secret(), nonce)) is False

    def test_response_to_a_different_nonce_fails(self):
        # This is what stops a recorded session being replayed later.
        secret = new_secret()
        stale = respond(secret, new_nonce())
        assert verify(secret, new_nonce(), stale) is False

    def test_response_is_deterministic(self):
        secret, nonce = new_secret(), new_nonce()
        assert respond(secret, nonce) == respond(secret, nonce)

    def test_response_is_hex_sha256(self):
        response = respond(new_secret(), new_nonce())
        assert len(response) == 64
        int(response, 16)


class TestVerifyIsHostileInputSafe:
    # A peer that hasn't authenticated yet is untrusted by definition, so
    # none of these may raise — each is just a failed authentication.
    def test_none_response(self):
        assert verify(new_secret(), new_nonce(), None) is False

    def test_non_string_response(self):
        assert verify(new_secret(), new_nonce(), 12345) is False
        assert verify(new_secret(), new_nonce(), {"mac": "x"}) is False
        assert verify(new_secret(), new_nonce(), ["x"]) is False

    def test_empty_response(self):
        assert verify(new_secret(), new_nonce(), "") is False

    def test_garbage_response(self):
        assert verify(new_secret(), new_nonce(), "not a mac") is False

    def test_truncated_correct_response(self):
        secret, nonce = new_secret(), new_nonce()
        full = respond(secret, nonce)
        assert verify(secret, nonce, full[:-1]) is False

    def test_malformed_nonce_fails_rather_than_raising(self):
        assert verify(new_secret(), "!!!", "abcd") is False

    def test_malformed_secret_fails_rather_than_raising(self):
        assert verify("!!!", new_nonce(), "abcd") is False
