"""The Entra bearer edge, and the resolution order behind it.

Tokens here are real — signed in-process with a throwaway RSA key — so the RS256
whitelist, the issuer pin, the audience pin and the tenant check are exercised
rather than asserted about. Only the key *fetch* is stubbed; nothing reaches
Microsoft.

This file used to cover Google IAP too. That edge is gone (see the last test),
so the coverage it had moved onto the edge that is left rather than being
deleted with it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException

from ask_maurice.config import EntraConfig, RuntimeConfig, SlackConfig
from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime.server import resolve_caller, verify

TENANT_ID = "11111111-2222-3333-4444-555555555555"
ENTRA = EntraConfig(
    tenant_id=TENANT_ID,
    audience="api://ask-maurice",
    resource_url="https://ask-maurice.example.com",
)
SLACK_SIGNING_SECRET = "test-slack-signing-secret"  # noqa: S105 - not a credential
SLACK_BOT_TOKEN = "test-slack-bot-token"  # noqa: S105 - not a credential
SLACK = SlackConfig(signing_secret=SLACK_SIGNING_SECRET, bot_token=SLACK_BOT_TOKEN)


_BASE = RuntimeConfig(
    corpus_path=Path("/nonexistent"),
    corpus_remote="git@github.com:Soilytix/vault.git",
    corpus_ref="main",
    include_transcripts=False,
    bundle_source="file",
    bundle_path=Path("/nonexistent/bundle.json"),
    bundle_secret="",
    entra=None,
    slack=None,
    mixedbread=None,
    production=False,
)


def _config(*, entra: EntraConfig | None = None, slack: SlackConfig | None = None) -> RuntimeConfig:
    """The base config with only the access edges varied — the rest is inert here."""
    return replace(_BASE, entra=entra, slack=slack)


@pytest.fixture
def entra_key(monkeypatch: pytest.MonkeyPatch) -> rsa.RSAPrivateKey:
    """A stand-in for the tenant's signing key, wired into the JWKS lookup."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _Client:
        def __init__(self, uri: str):
            assert uri == ENTRA.jwks_uri

        def get_signing_key_from_jwt(self, token: str):
            return type("Key", (), {"key": key.public_key()})()

    monkeypatch.setattr(jwt, "PyJWKClient", _Client)
    return key


def _entra_token(
    key, *, audience: str = ENTRA.audience, issuer: str | None = None, **claims
) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": issuer if issuer is not None else ENTRA.issuer,
        "aud": audience,
        "tid": TENANT_ID,
        "preferred_username": "ada@testco.com",
        "oid": "00000000-aaaa",
        "iat": now - timedelta(seconds=30),
        "exp": now + timedelta(minutes=5),
        **claims,
    }
    return jwt.encode(payload, key, algorithm="RS256")


# --- verify ------------------------------------------------------------------


def test_a_well_formed_bearer_token_verifies(entra_key):
    claims = verify(_entra_token(entra_key), ENTRA)
    assert claims["preferred_username"] == "ada@testco.com"
    assert claims["tid"] == TENANT_ID


def test_a_token_for_another_audience_is_rejected(entra_key):
    with pytest.raises(jwt.InvalidAudienceError):
        verify(_entra_token(entra_key, audience="api://some-other-app"), ENTRA)


def test_a_token_from_another_issuer_is_rejected(entra_key):
    with pytest.raises(jwt.InvalidIssuerError):
        verify(_entra_token(entra_key, issuer="https://cloud.google.com/iap"), ENTRA)


def test_a_token_from_another_tenant_is_rejected(entra_key):
    """The check the audience pin does not make: a guest tenant minting our
    audience is a different organisation's user, not ours."""
    other = "99999999-8888-7777-6666-555555555555"
    with pytest.raises(jwt.InvalidTokenError):
        verify(_entra_token(entra_key, tid=other), ENTRA)


def test_an_expired_token_is_rejected(entra_key):
    now = datetime.now(UTC)
    token = _entra_token(entra_key, iat=now - timedelta(hours=2), exp=now - timedelta(hours=1))
    with pytest.raises(jwt.ExpiredSignatureError):
        verify(token, ENTRA)


def test_an_unsigned_token_is_rejected(entra_key):
    """`alg: none` is the oldest trick there is; the RS256 whitelist stops it."""
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": ENTRA.issuer,
            "aud": ENTRA.audience,
            "tid": TENANT_ID,
            "preferred_username": "attacker@testco.com",
            "exp": now + timedelta(minutes=5),
        },
        key="",  # PyJWT's spelling of "no key" for the `none` algorithm
        algorithm="none",
    )
    with pytest.raises(jwt.PyJWTError):
        verify(token, ENTRA)


def test_an_es256_token_is_rejected_even_when_otherwise_valid(monkeypatch: pytest.MonkeyPatch):
    """Wrong algorithm for this edge, whatever the claims say."""
    ec_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": ENTRA.issuer,
            "aud": ENTRA.audience,
            "tid": TENANT_ID,
            "preferred_username": "ada@testco.com",
            "exp": now + timedelta(minutes=5),
        },
        ec_key,
        algorithm="ES256",
    )

    class _Client:
        def __init__(self, uri: str): ...

        def get_signing_key_from_jwt(self, token: str):
            return type("Key", (), {"key": ec_key.public_key()})()

    monkeypatch.setattr(jwt, "PyJWKClient", _Client)
    with pytest.raises(jwt.InvalidAlgorithmError):
        verify(token, ENTRA)


def test_a_token_missing_required_claims_is_rejected(entra_key):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"iss": ENTRA.issuer, "tid": TENANT_ID, "exp": now + timedelta(minutes=5)},
        entra_key,
        algorithm="RS256",
    )
    with pytest.raises(jwt.PyJWTError):
        verify(token, ENTRA)


# --- resolution order: bearer -> anonymous -----------------------------------


def test_a_valid_bearer_resolves_the_caller(entra_key, bundle: PersonaBundle):
    who = resolve_caller(f"Bearer {_entra_token(entra_key)}", _config(entra=ENTRA), bundle)
    assert who.known
    assert who.handle == "ada@testco.com"


def test_an_unknown_bearer_caller_is_answered_unframed_not_refused(
    entra_key, bundle: PersonaBundle
):
    token = _entra_token(entra_key, preferred_username="stranger@testco.com")
    who = resolve_caller(f"Bearer {token}", _config(entra=ENTRA), bundle)
    assert not who.known
    assert who.handle == "stranger@testco.com"


def test_a_bad_bearer_is_a_401_not_a_downgrade(entra_key, bundle: PersonaBundle):
    token = _entra_token(entra_key, audience="api://some-other-app")
    with pytest.raises(HTTPException) as caught:
        resolve_caller(f"Bearer {token}", _config(entra=ENTRA), bundle)
    assert caught.value.status_code == 401


def test_a_missing_bearer_is_refused_when_entra_is_the_only_edge(bundle: PersonaBundle):
    with pytest.raises(HTTPException) as caught:
        resolve_caller("", _config(entra=ENTRA), bundle)
    assert caught.value.status_code == 401


def test_ask_is_refused_on_a_slack_only_deploy(bundle: PersonaBundle):
    """Nothing can satisfy `/ask` there, and 401 is the right answer anyway: the
    team's surface is the slash command, and an anonymous `/ask` would hand out
    unframed answers to whoever found the URL."""
    with pytest.raises(HTTPException) as caught:
        resolve_caller("", _config(slack=SLACK), bundle)
    assert caught.value.status_code == 401


def test_anonymous_survives_only_with_no_edge_configured(bundle: PersonaBundle):
    who = resolve_caller("", _config(), bundle)
    assert who.handle == "anonymous"
    assert not who.known


def test_the_iap_edge_is_gone(bundle: PersonaBundle):
    """IAP fronted the whole Cloud Run service, so it also intercepted
    `/slack/command`, which Slack cannot sign in to. The edges were exclusive in
    practice and Slack is the surface we chose.

    What this asserts is that removing it left nothing half-connected: no
    verifier, no header constant, and no way to reach `resolve_caller` with an
    assertion. A stale `x-goog-iap-jwt-assertion` on an inbound request is now
    just a header nobody reads.
    """
    import inspect

    from ask_maurice.runtime import identity, server

    assert not hasattr(server, "verify_iap")
    assert not hasattr(server, "IAP_HEADER")
    assert not hasattr(identity, "from_iap_claims")
    # The assertion parameter is gone, so no route can pass one even by accident.
    assert list(inspect.signature(resolve_caller).parameters) == [
        "authorization",
        "config",
        "persona",
    ]
