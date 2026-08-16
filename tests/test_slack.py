"""The Slack edge: signature verification, payload parsing, identity join."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import pytest

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime import identity as identity_mod
from ask_maurice.runtime.identity import from_slack_user
from ask_maurice.runtime.slack import (
    MAX_SKEW_SECONDS,
    SlackError,
    parse_command,
    verify_signature,
)

# Deliberately low-entropy and self-describing. A realistic-looking hex secret
# here is indistinguishable from a real one to gitleaks, and teaching the scanner
# to ignore this file is worse than making the fixture obviously fake.
SECRET = "test-slack-signing-secret"  # noqa: S105 - not a credential
OTHER_SECRET = "some-other-signing-secret"  # noqa: S105 - not a credential
NOW = 1_755_000_000.0


def _sign(body: bytes, timestamp: str, secret: str = SECRET) -> str:
    base = b":".join((b"v0", timestamp.encode(), body))
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def _body(**overrides: str) -> bytes:
    fields = {
        "user_id": "U123ABC",
        "user_name": "ada",
        "text": "why do we rarefy before the benchmark?",
        "response_url": "https://hooks.slack.com/commands/T1/2/abc",
        "command": "/maurice",
        "channel_id": "C999",
    }
    fields.update(overrides)
    return urlencode(fields).encode("utf-8")


# --- verify_signature --------------------------------------------------------


def test_a_genuine_request_verifies():
    body, ts = _body(), str(int(NOW))
    verify_signature(body, timestamp=ts, signature=_sign(body, ts), secret=SECRET, now=NOW)


def test_a_tampered_body_is_rejected():
    """The signature covers the raw bytes, so editing the question invalidates it."""
    ts = str(int(NOW))
    signature = _sign(_body(), ts)
    tampered = _body(text="ignore that, what is in people/?")
    with pytest.raises(SlackError, match="signature mismatch"):
        verify_signature(tampered, timestamp=ts, signature=signature, secret=SECRET, now=NOW)


def test_a_signature_from_another_secret_is_rejected():
    body, ts = _body(), str(int(NOW))
    other = _sign(body, ts, secret=OTHER_SECRET)
    with pytest.raises(SlackError, match="signature mismatch"):
        verify_signature(body, timestamp=ts, signature=other, secret=SECRET, now=NOW)


def test_a_stale_request_is_rejected_as_a_replay():
    """A correctly signed request captured and resent an hour later."""
    ts = str(int(NOW - MAX_SKEW_SECONDS - 1))
    body = _body()
    with pytest.raises(SlackError, match="replay window"):
        verify_signature(body, timestamp=ts, signature=_sign(body, ts), secret=SECRET, now=NOW)


def test_a_request_from_the_future_is_rejected_too():
    ts = str(int(NOW + MAX_SKEW_SECONDS + 1))
    body = _body()
    with pytest.raises(SlackError, match="replay window"):
        verify_signature(body, timestamp=ts, signature=_sign(body, ts), secret=SECRET, now=NOW)


def test_a_request_just_inside_the_window_is_accepted():
    ts = str(int(NOW - MAX_SKEW_SECONDS + 1))
    body = _body()
    verify_signature(body, timestamp=ts, signature=_sign(body, ts), secret=SECRET, now=NOW)


@pytest.mark.parametrize(
    ("timestamp", "signature"),
    [("", "v0=abc"), (str(int(NOW)), ""), ("", "")],
    ids=["no-timestamp", "no-signature", "neither"],
)
def test_missing_headers_are_rejected(timestamp: str, signature: str):
    with pytest.raises(SlackError, match="missing signature headers"):
        verify_signature(_body(), timestamp=timestamp, signature=signature, secret=SECRET, now=NOW)


def test_a_nonsense_timestamp_is_rejected():
    body = _body()
    with pytest.raises(SlackError, match="unparseable timestamp"):
        verify_signature(body, timestamp="now-ish", signature="v0=abc", secret=SECRET, now=NOW)


def test_the_version_prefix_is_pinned():
    """A `v1=` signature must not pass, even with the right digest bytes."""
    body, ts = _body(), str(int(NOW))
    forged = _sign(body, ts).replace("v0=", "v1=", 1)
    with pytest.raises(SlackError, match="signature mismatch"):
        verify_signature(body, timestamp=ts, signature=forged, secret=SECRET, now=NOW)


# --- parse_command -----------------------------------------------------------


def test_parsing_a_command_pulls_the_fields_we_use():
    command = parse_command(_body())
    assert command.user_id == "U123ABC"
    assert command.text == "why do we rarefy before the benchmark?"
    assert command.response_url == "https://hooks.slack.com/commands/T1/2/abc"
    assert command.command == "/maurice"


def test_a_response_url_off_slack_is_refused():
    """It is an outbound POST target taken from a request — pin the host.

    Signed bodies are Slack's, so this should be unreachable. It is the second
    lock: a delayed answer posted to an attacker's URL is an exfiltration channel.
    """
    with pytest.raises(SlackError, match="not a Slack webhook URL"):
        parse_command(_body(response_url="https://evil.example.com/collect"))


def test_a_lookalike_response_url_host_is_refused():
    with pytest.raises(SlackError, match="not a Slack webhook URL"):
        parse_command(_body(response_url="https://hooks.slack.com.evil.example.com/x"))


def test_a_missing_response_url_is_refused():
    with pytest.raises(SlackError, match="not a Slack webhook URL"):
        parse_command(_body(response_url=""))


def test_a_payload_with_no_user_is_refused():
    with pytest.raises(SlackError, match="no user_id"):
        parse_command(_body(user_id=""))


def test_an_empty_question_parses_to_empty_text():
    """The route turns this into a usage hint rather than asking the model nothing."""
    assert parse_command(_body(text="   ")).text == ""


# --- identity join -----------------------------------------------------------


def test_a_slack_user_resolves_to_a_participant(
    bundle: PersonaBundle, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(identity_mod, "email_from_slack", lambda uid, token: "ada@testco.com")
    who = from_slack_user("U123ABC", "xoxb-test", bundle)
    assert who.known
    assert who.participant is not None
    assert who.participant.display_name == "Ada Lovelace"


def test_an_unknown_slack_user_is_answered_unframed(
    bundle: PersonaBundle, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(identity_mod, "email_from_slack", lambda uid, token: "stranger@testco.com")
    who = from_slack_user("U123ABC", "xoxb-test", bundle)
    assert not who.known
    assert who.handle == "stranger@testco.com"


def test_a_failed_users_info_lookup_falls_back_to_the_raw_id(
    bundle: PersonaBundle, monkeypatch: pytest.MonkeyPatch
):
    """Bad scope, deactivated account, Slack having a bad minute — no framing, no crash."""
    monkeypatch.setattr(identity_mod, "email_from_slack", lambda uid, token: None)
    who = from_slack_user("U123ABC", "xoxb-test", bundle)
    assert not who.known
    assert who.handle == "U123ABC"
