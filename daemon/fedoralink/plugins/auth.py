"""Gatekeeper: decide whether a connected device may speak to us.

The desktop is the authority — it owns the trust store, so it drives both
enrollment and the per-connection challenge. The phone answers challenges
and issues one of its own, so neither side takes the other on faith.

State lives per connection and is wiped on disconnect. There is no
"half authenticated": until ``daemon.authenticated`` is true, the daemon
drops every inbound packet that isn't identity or auth, and refuses to
send anything else.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import auth
from ..devices import DeviceStore
from ..protocol import AUTH
from . import Plugin

log = logging.getLogger(__name__)

STAGE_ENROLL = "enroll"
STAGE_CHALLENGE = "challenge"
STAGE_RESPONSE = "response"
STAGE_OK = "ok"
STAGE_FAIL = "fail"

APPROVE_ACTION = "fedoralink-approve"
REJECT_ACTION = "fedoralink-reject"


class AuthPlugin(Plugin):
    name = "auth"
    handles = (AUTH,)

    def __init__(self, daemon, store: DeviceStore | None = None) -> None:
        super().__init__(daemon)
        self.store = store if store is not None else DeviceStore()

        self._peer_id: str | None = None
        self._peer_name: str = ""
        # The challenge we issued and are waiting on.
        self._our_nonce: str | None = None
        # Secret offered to a new device, held until the user approves it.
        self._pending_secret: str | None = None
        self._prompt_id: int | None = None

    # ------------------------------------------------------------ lifecycle

    def on_disconnected(self, connection) -> None:
        self._reset()

    def _reset(self) -> None:
        self.daemon.notifications.cancel_ask(self._prompt_id)
        self._prompt_id = None
        self._peer_id = None
        self._peer_name = ""
        self._our_nonce = None
        self._pending_secret = None

    # ----------------------------------------------------------- entry point

    def begin(self, device_id: Any, device_name: str) -> None:
        """Called by the daemon once the peer's identity packet arrives."""
        if not isinstance(device_id, str) or not device_id:
            # Every build that speaks version 2 sends one. A peer without
            # it cannot be told apart from any other, so it cannot be
            # trusted.
            self._refuse("peer sent no deviceId")
            return

        self._peer_id = device_id
        self._peer_name = device_name or device_id

        secret = self.store.secret_for(device_id)
        if secret is None:
            self._start_enrollment()
        else:
            self._challenge(secret)

    # ---------------------------------------------------------- enrollment

    def _start_enrollment(self) -> None:
        secret = auth.new_secret()
        self._pending_secret = secret
        code = auth.fingerprint(secret)

        log.info("unknown device %s; asking to enroll", self._peer_name)

        # The secret crosses a link Bluetooth has already encrypted. The
        # code is for the human: it catches a different device having
        # answered, which encryption alone would not.
        self.send(STAGE_ENROLL, {"secret": secret, "code": code})

        self._prompt_id = self.daemon.notifications.ask(
            summary=f"Link with {self._peer_name}?",
            body=(
                f"Confirm this code matches the one on your phone: {code}\n"
                "Approving grants access to your clipboard and notifications."
            ),
            actions=[(APPROVE_ACTION, "Approve"), (REJECT_ACTION, "Reject")],
            on_action=self._enrollment_answered,
        )

        if self._prompt_id is None:
            # No notification server, so nobody can approve anything.
            # Refusing beats trusting silently.
            self._refuse("could not ask the user to approve the device")

    def _enrollment_answered(self, action: str | None) -> None:
        self._prompt_id = None
        device_id, secret = self._peer_id, self._pending_secret
        self._pending_secret = None

        if action != APPROVE_ACTION:
            self._refuse(
                "rejected by the user"
                if action == REJECT_ACTION
                else "approval dialog dismissed"
            )
            return

        if device_id is None or secret is None:
            # The phone disconnected while the prompt was on screen.
            log.info("device went away before approval completed")
            return

        self.store.trust(device_id, self._peer_name, secret)
        self._succeed()

    # ------------------------------------------------- challenge / response

    def _challenge(self, secret: str) -> None:
        self._our_nonce = auth.new_nonce()
        log.debug("challenging %s", self._peer_name)
        self.send(STAGE_CHALLENGE, {"nonce": self._our_nonce})

    def on_packet(self, packet: dict[str, Any]) -> None:
        body = packet["body"]
        stage = body.get("stage")

        if stage == STAGE_RESPONSE:
            self._check_response(body.get("mac"))
        elif stage == STAGE_CHALLENGE:
            self._answer_challenge(body.get("nonce"))
        elif stage == STAGE_FAIL:
            log.error(
                "%s rejected this PC: %s",
                self._peer_name or "peer",
                body.get("reason") or "no reason given",
            )
            self.daemon.drop_connection("rejected by the phone")
        elif stage == STAGE_OK:
            log.debug("peer accepted our response")
        else:
            log.warning("unknown auth stage %r", stage)

    def _check_response(self, mac: Any) -> None:
        nonce, device_id = self._our_nonce, self._peer_id
        # One challenge, one answer. Clearing it first stops a peer
        # brute-forcing MACs against a nonce we keep accepting.
        self._our_nonce = None

        if nonce is None or device_id is None:
            self._refuse("unexpected auth response")
            return

        secret = self.store.secret_for(device_id)
        if secret is None or not auth.verify(secret, nonce, mac):
            self._refuse("challenge response did not verify")
            return

        self._succeed()

    def _answer_challenge(self, nonce: Any) -> None:
        """The phone is checking us. Only a trusted peer gets an answer."""
        if not isinstance(nonce, str) or not nonce:
            log.warning("peer sent a malformed challenge")
            return

        device_id = self._peer_id
        secret = self.store.secret_for(device_id) if device_id else None
        if secret is None:
            secret = self._pending_secret

        if secret is None:
            log.warning("cannot answer a challenge from an unknown device")
            return

        try:
            mac = auth.respond(secret, nonce)
        except ValueError as exc:
            log.warning("could not answer challenge: %s", exc)
            return

        self.send(STAGE_RESPONSE, {"mac": mac})

    # -------------------------------------------------------------- outcome

    def _succeed(self) -> None:
        log.info("authenticated %s", self._peer_name)
        self.send(STAGE_OK, {})
        self.daemon.set_authenticated(True)

    def _refuse(self, reason: str) -> None:
        log.warning("refusing %s: %s", self._peer_name or "peer", reason)
        self.send(STAGE_FAIL, {"reason": reason})
        self.daemon.drop_connection(reason)

    # --------------------------------------------------------------- helper

    def send(self, stage: str, body: dict[str, Any]) -> bool:
        return self.daemon.send(AUTH, {"stage": stage, **body})
