"""The two access edges: IAP assertion verification, and the resolution order.

Tokens here are real — signed in-process with a throwaway EC key — so the ES256
whitelist, the issuer pin and the audience pin are exercised rather than asserted
about. Only the key *fetch* is stubbed; nothing reaches Google.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException

from ask_maurice.config import EntraConfig, IapConfig, RuntimeConfig
from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime import server
from ask_maurice.runtime.server import resolve_caller, verify_iap

IAP_AUDIENCE = "/projects/123456789012/locations/europe-west3/services/ask-maurice"
IAP_ISSUER = "https://cloud.google.com/iap"

ENTRA = EntraConfig(
    tenant_id="11111111-2222-3333-4444-555555555555",
    audience="api://ask-maurice",
    resource_url="https://ask-maurice.example.com",
)
IAP = IapConfig(audience=IAP_AUDIENCE)


_BASE = RuntimeConfig(
    corpus_path=Path("/nonexistent"),
    corpus_remote="git@github.com:Soilytix/vault.git",
    corpus_ref="main",
    include_transcripts=False,
    bundle_source="file",
    bundle_path=Path("/nonexistent/bundle.json"),
    bundle_secret="",
    entra=None,
    iap=None,
    production=False,
)


def _config(*, entra: EntraConfig | None = None, iap: IapConfig | None = None) -> RuntimeConfig:
    """The base config with only the access edges varied — the rest is inert here."""
    return replace(_BASE, entra=entra, iap=iap)


@pytest.fixture
def iap_key(monkeypatch: pytest.MonkeyPatch) -> ec.EllipticCurvePrivateKey:
    """A stand-in for Google's IAP signing key, wired into the JWKS lookup."""
    key = ec.generate_private_key(ec.SECP256R1())

    class _Client:
        def __init__(self, uri: str):
            assert uri == IapConfig.JWKS_URI

        def get_signing_key_from_jwt(self, token: str):
            return type("Key", (), {"key": key.public_key()})()

    monkeypatch.setattr(jwt, "PyJWKClient", _Client)
    return key


def _iap_token(key, *, audience: str = IAP_AUDIENCE, issuer: str = IAP_ISSUER, **claims) -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": issuer,
        "aud": audience,
        "email": "ada@testco.com",
        "sub": "accounts.google.com:1234",
        "iat": now - timedelta(seconds=30),
        "exp": now + timedelta(minutes=5),
        **claims,
    }
    return jwt.encode(payload, key, algorithm="ES256")


# --- verify_iap --------------------------------------------------------------


def test_a_well_formed_assertion_verifies(iap_key):
    claims = verify_iap(_iap_token(iap_key), IAP)
    assert claims["email"] == "ada@testco.com"
    assert claims["iss"] == IAP_ISSUER


def test_an_assertion_for_another_service_is_rejected(iap_key):
    """The load-balancer audience format, against a Cloud Run service."""
    token = _iap_token(iap_key, audience="/projects/123456789012/global/backendServices/42")
    with pytest.raises(jwt.InvalidAudienceError):
        verify_iap(token, IAP)


def test_an_assertion_from_another_issuer_is_rejected(iap_key):
    token = _iap_token(iap_key, issuer="https://login.microsoftonline.com/tenant/v2.0")
    with pytest.raises(jwt.InvalidIssuerError):
        verify_iap(token, IAP)


def test_an_expired_assertion_is_rejected(iap_key):
    now = datetime.now(UTC)
    token = _iap_token(iap_key, iat=now - timedelta(hours=2), exp=now - timedelta(hours=1))
    with pytest.raises(jwt.ExpiredSignatureError):
        verify_iap(token, IAP)


def test_an_unsigned_assertion_is_rejected(iap_key):
    """`alg: none` is the oldest trick there is; the ES256 whitelist stops it."""
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": IAP_ISSUER,
            "aud": IAP_AUDIENCE,
            "email": "attacker@testco.com",
            "exp": now + timedelta(minutes=5),
        },
        key="",  # PyJWT's spelling of "no key" for the `none` algorithm
        algorithm="none",
    )
    with pytest.raises(jwt.PyJWTError):
        verify_iap(token, IAP)


def test_an_rs256_token_is_rejected_even_when_otherwise_valid(monkeypatch: pytest.MonkeyPatch):
    """Wrong algorithm for this edge — Entra's shape must not pass as IAP's."""
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": IAP_ISSUER,
            "aud": IAP_AUDIENCE,
            "email": "ada@testco.com",
            "exp": now + timedelta(minutes=5),
        },
        rsa_key,
        algorithm="RS256",
    )

    class _Client:
        def __init__(self, uri: str): ...

        def get_signing_key_from_jwt(self, token: str):
            return type("Key", (), {"key": rsa_key.public_key()})()

    monkeypatch.setattr(jwt, "PyJWKClient", _Client)
    with pytest.raises(jwt.InvalidAlgorithmError):
        verify_iap(token, IAP)


def test_an_assertion_missing_required_claims_is_rejected(iap_key):
    now = datetime.now(UTC)
    token = jwt.encode(
        {"iss": IAP_ISSUER, "email": "ada@testco.com", "exp": now + timedelta(minutes=5)},
        iap_key,
        algorithm="ES256",
    )
    with pytest.raises(jwt.PyJWTError):
        verify_iap(token, IAP)


# --- resolution order: bearer -> IAP -> anonymous ----------------------------


def test_iap_header_resolves_the_caller_when_there_is_no_bearer(iap_key, bundle: PersonaBundle):
    who = resolve_caller("", _iap_token(iap_key), _config(iap=IAP), bundle)
    assert who.known
    assert who.handle == "ada@testco.com"


def test_an_unknown_iap_caller_is_answered_unframed_not_refused(iap_key, bundle: PersonaBundle):
    token = _iap_token(iap_key, email="stranger@testco.com")
    who = resolve_caller("", token, _config(iap=IAP), bundle)
    assert not who.known
    assert who.handle == "stranger@testco.com"


def test_a_valid_bearer_wins_over_a_valid_iap_assertion(
    iap_key, bundle: PersonaBundle, monkeypatch: pytest.MonkeyPatch
):
    """Behind IAP every request carries an assertion; a bearer token is the
    deliberate one, so it is the identity we honour."""
    monkeypatch.setattr(server, "verify", lambda token, config: {"email": "grace@testco.com"})
    who = resolve_caller(
        "Bearer entra-token", _iap_token(iap_key), _config(entra=ENTRA, iap=IAP), bundle
    )
    assert who.handle == "grace@testco.com"


def test_a_bad_iap_assertion_is_a_401_not_a_downgrade(iap_key, bundle: PersonaBundle):
    token = _iap_token(iap_key, audience="/projects/999/locations/x/services/other")
    with pytest.raises(HTTPException) as caught:
        resolve_caller("", token, _config(iap=IAP), bundle)
    assert caught.value.status_code == 401


def test_a_missing_assertion_is_refused_when_iap_is_the_only_edge(bundle: PersonaBundle):
    with pytest.raises(HTTPException) as caught:
        resolve_caller("", "", _config(iap=IAP), bundle)
    assert caught.value.status_code == 401


def test_a_missing_bearer_is_still_refused_when_entra_is_the_only_edge(bundle: PersonaBundle):
    with pytest.raises(HTTPException) as caught:
        resolve_caller("", "", _config(entra=ENTRA), bundle)
    assert caught.value.status_code == 401


def test_an_iap_assertion_is_ignored_when_iap_is_not_configured(iap_key, bundle: PersonaBundle):
    """An unpinnable header must never become an identity. With no IAP audience
    configured there is nothing to verify the assertion against, so it is not an
    access edge — the Entra-only service refuses rather than trusting it."""
    with pytest.raises(HTTPException) as caught:
        resolve_caller("", _iap_token(iap_key), _config(entra=ENTRA), bundle)
    assert caught.value.status_code == 401


def test_anonymous_survives_only_with_no_edge_configured(bundle: PersonaBundle):
    who = resolve_caller("", "", _config(), bundle)
    assert who.handle == "anonymous"
    assert not who.known
