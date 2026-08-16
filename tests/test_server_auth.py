"""What `/ask` does now that no HTTP edge authenticates anybody.

This file used to verify real Entra and IAP tokens. Both edges are gone, so what
is left to pin is the policy that replaced them: `/ask` answers anonymously on a
machine where nobody can be identified, and refuses everywhere else. Getting that
backwards is the failure worth a test — an open `/ask` on a deployed service
hands unframed answers to whoever finds the URL.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException

from ask_maurice.config import RuntimeConfig, SlackConfig
from ask_maurice.runtime.server import resolve_caller

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
    slack=None,
    mixedbread=None,
    production=False,
)


def _config(*, slack: SlackConfig | None = None) -> RuntimeConfig:
    """The base config with only the access edge varied — the rest is inert here."""
    return replace(_BASE, slack=slack)


def test_anonymous_only_with_no_edge_configured():
    """The local case: nothing can identify anyone, so `/ask` is open and every
    answer is unframed. `ask --as` is how a developer gets framing back."""
    who = resolve_caller(_config())
    assert who.handle == "anonymous"
    assert not who.known


def test_ask_is_refused_once_any_edge_exists():
    """A Slack-configured deployment. Nothing can satisfy `/ask` — that is the
    point: the team's surface is the slash command, and an anonymous `/ask` on a
    reachable service would be the hole the boot guard exists to prevent."""
    with pytest.raises(HTTPException) as caught:
        resolve_caller(_config(slack=SLACK))
    assert caught.value.status_code == 401


def test_the_refusal_names_the_surface_that_does_work():
    """A 401 with no route forward reads as a broken deploy. This one says where
    to go instead."""
    with pytest.raises(HTTPException) as caught:
        resolve_caller(_config(slack=SLACK))
    assert "slack" in str(caught.value.detail).lower()


def test_the_bearer_edges_are_gone(bundle):
    """Entra and IAP are both removed, and this fails if either half returns.

    Neither was deleted for being broken. IAP fronted the whole Cloud Run service
    and would have intercepted `/slack/command`, which Slack cannot sign in to;
    Entra worked but served nothing on the first surface, so it was carrying
    config, a dependency and a verifier for a caller that does not exist yet.

    What this asserts is that removing them left nothing half-connected: no
    verifiers, no header constant, no claim-shaped identity join, and a
    `resolve_caller` that takes the config alone — there is no token parameter
    for a route to pass, so no route can accidentally start trusting one.
    """
    import inspect

    from ask_maurice.runtime import identity, server

    assert not hasattr(server, "verify")
    assert not hasattr(server, "verify_iap")
    assert not hasattr(server, "IAP_HEADER")
    assert not hasattr(identity, "from_claims")
    assert not hasattr(identity, "from_iap_claims")
    assert list(inspect.signature(resolve_caller).parameters) == ["config"]
    # The RFC 9728 discovery route existed only to point MCP clients at Entra.
    assert not hasattr(server, "protected_resource")


def test_no_jwt_verification_remains_in_the_runtime():
    """The whole PyJWT surface went with the two edges. If a module grows an
    `import jwt` again it is because a bearer edge is coming back, and that is a
    decision to make on purpose rather than discover in a diff."""
    import importlib.util

    assert importlib.util.find_spec("jwt") is None
