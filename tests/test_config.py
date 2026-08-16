"""The production access-edge guard, and the two ways to satisfy it."""

from __future__ import annotations

import pytest

from ask_maurice.config import ConfigError, IapConfig, RuntimeConfig

ENTRA_VARS = {
    "ASK_MAURICE_ENTRA_TENANT_ID": "11111111-2222-3333-4444-555555555555",
    "ASK_MAURICE_ENTRA_AUDIENCE": "api://ask-maurice",
    "ASK_MAURICE_RESOURCE_URL": "https://ask-maurice.example.com",
}
# Cloud Run's form: leading slash, project NUMBER, location, service name.
IAP_AUDIENCE = "/projects/123456789012/locations/europe-west3/services/ask-maurice"

# Low-entropy and unprefixed on purpose: a realistic `xoxb-…` here trips the
# secret scanner, and the values are never parsed, only carried.
SLACK_VARS = {
    "ASK_MAURICE_SLACK_SIGNING_SECRET": "test-slack-signing-secret",
    "SLACK_BOT_TOKEN": "test-slack-bot-token",
}

ALL_VARS = [
    *ENTRA_VARS,
    *SLACK_VARS,
    "ASK_MAURICE_IAP_AUDIENCE",
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


def test_production_accepts_iap_alone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASK_MAURICE_IAP_AUDIENCE", IAP_AUDIENCE)
    config = RuntimeConfig.from_env()
    assert config.entra is None
    assert config.iap == IapConfig(audience=IAP_AUDIENCE)
    assert config.has_access_edge


def test_production_still_accepts_entra_alone(monkeypatch: pytest.MonkeyPatch):
    for name, value in ENTRA_VARS.items():
        monkeypatch.setenv(name, value)
    config = RuntimeConfig.from_env()
    assert config.iap is None
    assert config.entra is not None
    assert config.has_access_edge


def test_production_accepts_both_edges(monkeypatch: pytest.MonkeyPatch):
    for name, value in ENTRA_VARS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ASK_MAURICE_IAP_AUDIENCE", IAP_AUDIENCE)
    config = RuntimeConfig.from_env()
    assert config.entra is not None
    assert config.iap is not None


def test_production_accepts_slack_alone(monkeypatch: pytest.MonkeyPatch):
    """The v1 surface: Slack in front, no browser edge at all."""
    for name, value in SLACK_VARS.items():
        monkeypatch.setenv(name, value)
    config = RuntimeConfig.from_env()
    assert config.entra is None
    assert config.iap is None
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


def test_production_refuses_with_no_edge_and_names_all_three(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ConfigError) as caught:
        RuntimeConfig.from_env()
    message = str(caught.value)
    assert "ASK_MAURICE_ENTRA_TENANT_ID" in message
    assert "ASK_MAURICE_IAP_AUDIENCE" in message
    assert "ASK_MAURICE_SLACK_SIGNING_SECRET" in message


def test_development_still_runs_with_no_edge_at_all(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASK_MAURICE_ENV", "development")
    config = RuntimeConfig.from_env()
    assert not config.has_access_edge
    assert not config.production


def test_a_partial_entra_config_is_no_edge(monkeypatch: pytest.MonkeyPatch):
    """Two of three variables is a misconfiguration, not half an access edge."""
    monkeypatch.setenv("ASK_MAURICE_ENTRA_TENANT_ID", ENTRA_VARS["ASK_MAURICE_ENTRA_TENANT_ID"])
    monkeypatch.setenv("ASK_MAURICE_ENTRA_AUDIENCE", ENTRA_VARS["ASK_MAURICE_ENTRA_AUDIENCE"])
    with pytest.raises(ConfigError):
        RuntimeConfig.from_env()


def test_iap_endpoints_are_googles_and_not_configurable():
    iap = IapConfig(audience=IAP_AUDIENCE)
    assert iap.issuer == "https://cloud.google.com/iap"
    assert iap.jwks_uri == "https://www.gstatic.com/iap/verify/public_key-jwk"
