"""The gatekeeper's behaviour, including every way a peer can fail it."""

from __future__ import annotations

from fedoralink import auth
from fedoralink.devices import DeviceStore
from fedoralink.plugins.auth import (
    APPROVE_ACTION,
    REJECT_ACTION,
    STAGE_CHALLENGE,
    STAGE_ENROLL,
    STAGE_FAIL,
    STAGE_OK,
    STAGE_RESPONSE,
    AuthPlugin,
)
from fedoralink.protocol import AUTH, make_packet


class FakeNotifications:
    def __init__(self, available=True) -> None:
        self.available = available
        self.asked: list[dict] = []
        self.cancelled: list[int] = []
        self._next_id = 100
        self.on_action = None

    def ask(self, summary, body, actions, on_action, **_kw):
        if not self.available:
            return None
        self._next_id += 1
        self.asked.append({"summary": summary, "body": body, "actions": actions})
        self.on_action = on_action
        return self._next_id

    def cancel_ask(self, local_id):
        if local_id is not None:
            self.cancelled.append(local_id)


class FakeDaemon:
    def __init__(self, notifications_available=True) -> None:
        self.notifications = FakeNotifications(notifications_available)
        self.authenticated = False
        self.sent: list[tuple[str, dict]] = []
        self.dropped: list[str] = []

    def send(self, packet_type, body=None):
        self.sent.append((packet_type, body or {}))
        return True

    def set_authenticated(self, value):
        self.authenticated = value

    def drop_connection(self, reason):
        self.dropped.append(reason)


def build(tmp_path, **kwargs):
    daemon = FakeDaemon(**kwargs)
    store = DeviceStore(tmp_path / "devices.json")
    return daemon, AuthPlugin(daemon, store=store), store


def stages(daemon):
    return [b.get("stage") for t, b in daemon.sent if t == AUTH]


def deliver(plugin, **body):
    plugin.on_packet(make_packet(AUTH, body))


class TestEnrollment:
    def test_unknown_device_is_offered_a_secret_and_a_prompt(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        plugin.begin("dev-1", "Pixel")

        assert stages(daemon) == [STAGE_ENROLL]
        assert len(daemon.notifications.asked) == 1
        assert not daemon.authenticated

    def test_prompt_shows_the_same_code_that_was_sent(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        plugin.begin("dev-1", "Pixel")

        _, body = daemon.sent[0]
        assert body["code"] == auth.fingerprint(body["secret"])
        assert body["code"] in daemon.notifications.asked[0]["body"]

    def test_approval_stores_the_secret_and_authenticates(self, tmp_path):
        daemon, plugin, store = build(tmp_path)
        plugin.begin("dev-1", "Pixel")
        sent_secret = daemon.sent[0][1]["secret"]

        daemon.notifications.on_action(APPROVE_ACTION)

        assert store.secret_for("dev-1") == sent_secret
        assert daemon.authenticated is True
        assert stages(daemon)[-1] == STAGE_OK

    def test_approval_persists_across_a_restart(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        plugin.begin("dev-1", "Pixel")
        secret = daemon.sent[0][1]["secret"]
        daemon.notifications.on_action(APPROVE_ACTION)

        assert DeviceStore(tmp_path / "devices.json").secret_for("dev-1") == secret

    def test_rejection_drops_the_link_and_stores_nothing(self, tmp_path):
        daemon, plugin, store = build(tmp_path)
        plugin.begin("dev-1", "Pixel")
        daemon.notifications.on_action(REJECT_ACTION)

        assert store.is_trusted("dev-1") is False
        assert daemon.authenticated is False
        assert daemon.dropped
        assert stages(daemon)[-1] == STAGE_FAIL

    def test_dismissing_the_prompt_is_a_refusal(self, tmp_path):
        # A swiped-away dialog must not leave the peer half-connected
        # waiting, and must not count as approval.
        daemon, plugin, store = build(tmp_path)
        plugin.begin("dev-1", "Pixel")
        daemon.notifications.on_action(None)

        assert store.is_trusted("dev-1") is False
        assert daemon.dropped

    def test_no_notification_server_means_refuse(self, tmp_path):
        # Nobody can approve anything, so trusting silently would be the
        # one unacceptable outcome.
        daemon, plugin, store = build(tmp_path, notifications_available=False)
        plugin.begin("dev-1", "Pixel")

        assert daemon.dropped
        assert store.is_trusted("dev-1") is False

    def test_disconnect_during_the_prompt_stores_nothing(self, tmp_path):
        daemon, plugin, store = build(tmp_path)
        plugin.begin("dev-1", "Pixel")
        callback = daemon.notifications.on_action

        plugin.on_disconnected(None)
        callback(APPROVE_ACTION)

        assert store.is_trusted("dev-1") is False

    def test_disconnect_withdraws_the_prompt(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        plugin.begin("dev-1", "Pixel")
        plugin.on_disconnected(None)
        assert daemon.notifications.cancelled


class TestChallengeResponse:
    def enrolled(self, tmp_path):
        daemon, plugin, store = build(tmp_path)
        secret = auth.new_secret()
        store.trust("dev-1", "Pixel", secret)
        return daemon, plugin, store, secret

    def test_known_device_is_challenged_not_enrolled(self, tmp_path):
        daemon, plugin, _, _ = self.enrolled(tmp_path)
        plugin.begin("dev-1", "Pixel")

        assert stages(daemon) == [STAGE_CHALLENGE]
        assert daemon.notifications.asked == []

    def test_correct_response_authenticates(self, tmp_path):
        daemon, plugin, _, secret = self.enrolled(tmp_path)
        plugin.begin("dev-1", "Pixel")
        nonce = daemon.sent[0][1]["nonce"]

        deliver(plugin, stage=STAGE_RESPONSE, mac=auth.respond(secret, nonce))

        assert daemon.authenticated is True
        assert stages(daemon)[-1] == STAGE_OK

    def test_wrong_secret_is_refused(self, tmp_path):
        daemon, plugin, _, _ = self.enrolled(tmp_path)
        plugin.begin("dev-1", "Pixel")
        nonce = daemon.sent[0][1]["nonce"]

        wrong = auth.respond(auth.new_secret(), nonce)
        deliver(plugin, stage=STAGE_RESPONSE, mac=wrong)

        assert daemon.authenticated is False
        assert daemon.dropped
        assert stages(daemon)[-1] == STAGE_FAIL

    def test_missing_mac_is_refused(self, tmp_path):
        daemon, plugin, _, _ = self.enrolled(tmp_path)
        plugin.begin("dev-1", "Pixel")
        deliver(plugin, stage=STAGE_RESPONSE)
        assert daemon.dropped

    def test_non_string_mac_is_refused(self, tmp_path):
        daemon, plugin, _, _ = self.enrolled(tmp_path)
        plugin.begin("dev-1", "Pixel")
        deliver(plugin, stage=STAGE_RESPONSE, mac={"nope": 1})
        assert daemon.dropped

    def test_a_nonce_is_only_good_once(self, tmp_path):
        # Otherwise a peer can sit there grinding MACs against a challenge
        # we keep accepting.
        daemon, plugin, _, secret = self.enrolled(tmp_path)
        plugin.begin("dev-1", "Pixel")
        nonce = daemon.sent[0][1]["nonce"]
        good = auth.respond(secret, nonce)

        deliver(plugin, stage=STAGE_RESPONSE, mac="wrong")
        daemon.authenticated = False
        deliver(plugin, stage=STAGE_RESPONSE, mac=good)

        assert daemon.authenticated is False

    def test_response_without_a_challenge_is_refused(self, tmp_path):
        daemon, plugin, _, secret = self.enrolled(tmp_path)
        stale = auth.respond(secret, auth.new_nonce())
        deliver(plugin, stage=STAGE_RESPONSE, mac=stale)
        assert daemon.dropped

    def test_nonces_differ_between_connections(self, tmp_path):
        daemon, plugin, _, _ = self.enrolled(tmp_path)
        plugin.begin("dev-1", "Pixel")
        first = daemon.sent[0][1]["nonce"]
        plugin.on_disconnected(None)
        plugin.begin("dev-1", "Pixel")
        second = daemon.sent[-1][1]["nonce"]
        assert first != second


class TestAnsweringThePhonesChallenge:
    def test_trusted_device_gets_an_answer(self, tmp_path):
        daemon, plugin, store = build(tmp_path)
        secret = auth.new_secret()
        store.trust("dev-1", "Pixel", secret)
        plugin.begin("dev-1", "Pixel")
        daemon.sent.clear()

        nonce = auth.new_nonce()
        deliver(plugin, stage=STAGE_CHALLENGE, nonce=nonce)

        stage, body = daemon.sent[-1][1]["stage"], daemon.sent[-1][1]
        assert stage == STAGE_RESPONSE
        assert body["mac"] == auth.respond(secret, nonce)

    def test_unknown_device_gets_no_answer(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        daemon.sent.clear()
        deliver(plugin, stage=STAGE_CHALLENGE, nonce=auth.new_nonce())
        assert stages(daemon) == []

    def test_malformed_challenge_is_ignored(self, tmp_path):
        daemon, plugin, store = build(tmp_path)
        store.trust("dev-1", "Pixel", auth.new_secret())
        plugin.begin("dev-1", "Pixel")
        daemon.sent.clear()

        deliver(plugin, stage=STAGE_CHALLENGE, nonce=None)
        deliver(plugin, stage=STAGE_CHALLENGE, nonce="")
        assert stages(daemon) == []

    def test_enrolling_device_can_be_answered_before_approval(self, tmp_path):
        # The phone verifies us during enrollment, using the secret we just
        # offered. Refusing would deadlock the pairing.
        daemon, plugin, _ = build(tmp_path)
        plugin.begin("dev-1", "Pixel")
        secret = daemon.sent[0][1]["secret"]
        daemon.sent.clear()

        nonce = auth.new_nonce()
        deliver(plugin, stage=STAGE_CHALLENGE, nonce=nonce)
        assert daemon.sent[-1][1]["mac"] == auth.respond(secret, nonce)


class TestMalformedAndHostileInput:
    def test_no_device_id_is_refused(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        plugin.begin(None, "Pixel")
        assert daemon.dropped
        assert daemon.authenticated is False

    def test_empty_device_id_is_refused(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        plugin.begin("", "Pixel")
        assert daemon.dropped

    def test_non_string_device_id_is_refused(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        plugin.begin(12345, "Pixel")
        assert daemon.dropped

    def test_unknown_stage_is_ignored_not_fatal(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        deliver(plugin, stage="nonsense")
        assert daemon.dropped == []
        assert daemon.authenticated is False

    def test_missing_stage_is_ignored(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        deliver(plugin)
        assert daemon.authenticated is False

    def test_peer_rejecting_us_drops_the_link(self, tmp_path):
        daemon, plugin, _ = build(tmp_path)
        deliver(plugin, stage=STAGE_FAIL, reason="unknown PC")
        assert daemon.dropped

    def test_peer_ok_alone_does_not_authenticate(self, tmp_path):
        # Only our own verification may flip the gate. A peer claiming
        # success proves nothing.
        daemon, plugin, _ = build(tmp_path)
        deliver(plugin, stage=STAGE_OK)
        assert daemon.authenticated is False
