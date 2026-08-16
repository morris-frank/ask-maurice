"""The production access-edge guard, and the one way to satisfy it."""

from __future__ import annotations

import pytest

from ask_maurice.config import ConfigError, RuntimeConfig

# Low-entropy and unprefixed on purpose: a realistic `xoxb-…` here trips the
# secret scanner, and the values are never parsed, only carried.
SLACK_VARS = {
    "ASK_MAURICE_SLACK_SIGNING_SECRET": "test-slack-signing-secret",
    "SLACK_BOT_TOKEN": "test-slack-bot-token",
}

# The variables of the two removed edges. Cleared like the rest, because the
# point of several tests below is that setting them changes nothing.
RETIRED_VARS = [
    "ASK_MAURICE_ENTRA_TENANT_ID",
    "ASK_MAURICE_ENTRA_AUDIENCE",
    "ASK_MAURICE_RESOURCE_URL",
    "ASK_MAURICE_IAP_AUDIENCE",
]

ALL_VARS = [
    *SLACK_VARS,
    *RETIRED_VARS,
    "ASK_MAURICE_ENV",
    "ASK_MAURICE_BUNDLE_SOURCE",
    # `RuntimeConfig.from_env` also builds the mixedbread config, which raises on a
    # half-configured store. Cleared so these tests fail on the access-edge guard
    # and never on whatever retrieval variables the machine happens to carry —
    # see test_config_mixedbread.py for that half.
    "MXBAI_API_KEY",
    "ASK_MAURICE_LITERATURE_STORE",
    "ASK_MAURICE_VAULT_STORE",
    "ASK_MAURICE_VAULT_RETRIEVAL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ASK_MAURICE_ENV", "production")


def test_production_accepts_slack(monkeypatch: pytest.MonkeyPatch):
    """The v1 surface, and currently the only one."""
    for name, value in SLACK_VARS.items():
        monkeypatch.setenv(name, value)
    config = RuntimeConfig.from_env()
    assert config.slack is not None
    assert config.has_access_edge


def test_a_signing_secret_without_a_bot_token_is_no_edge(monkeypatch: pytest.MonkeyPatch):
    """Requests would verify and then frame nobody — a service that looks fine."""
    monkeypatch.setenv(
        "ASK_MAURICE_SLACK_SIGNING_SECRET", SLACK_VARS["ASK_MAURICE_SLACK_SIGNING_SECRET"]
    )
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env()


def test_a_bot_token_without_a_signing_secret_is_no_edge(monkeypatch: pytest.MonkeyPatch):
    """Worse: identity resolution with nothing authenticating the request."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", SLACK_VARS["SLACK_BOT_TOKEN"])
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env()


def test_production_refuses_with_no_edge_and_says_what_to_set(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ConfigError) as caught:
        RuntimeConfig.from_env()
    message = str(caught.value)
    assert "ASK_MAURICE_SLACK_SIGNING_SECRET" in message
    assert "SLACK_BOT_TOKEN" in message


def test_the_retired_edge_variables_are_read_by_nothing(monkeypatch: pytest.MonkeyPatch):
    """Entra and IAP are gone. A deploy still carrying their variables must get
    the boot refusal, not a service that looks configured and answers nobody.

    Silence is the dangerous outcome here: `ASK_MAURICE_ENTRA_*` set and honoured
    by nothing would read, from the console, exactly like a working auth edge.
    """
    monkeypatch.setenv("ASK_MAURICE_ENTRA_TENANT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("ASK_MAURICE_ENTRA_AUDIENCE", "api://ask-maurice")
    monkeypatch.setenv("ASK_MAURICE_RESOURCE_URL", "https://ask-maurice.example.com")
    monkeypatch.setenv(
        "ASK_MAURICE_IAP_AUDIENCE",
        "/projects/123456789012/locations/europe-west3/services/ask-maurice",
    )
    with pytest.raises(ConfigError) as caught:
        RuntimeConfig.from_env()
    message = str(caught.value)
    assert "Entra" not in message
    assert "IAP" not in message


def test_the_config_has_no_bearer_edge_fields():
    """`RuntimeConfig.entra` and `.iap` are gone rather than left set to None, so
    a caller that still reads them fails loudly instead of seeing 'unconfigured'."""
    fields = RuntimeConfig.__dataclass_fields__
    assert "entra" not in fields
    assert "iap" not in fields


def test_development_still_runs_with_no_edge_at_all(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASK_MAURICE_ENV", "development")
    config = RuntimeConfig.from_env()
    assert not config.has_access_edge
    assert not config.production
