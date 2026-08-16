"""The agent refuses to exist without a key, rather than 500ing on first use."""

from __future__ import annotations

from pathlib import Path

import pytest

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime.agent import Agent, AgentError
from ask_maurice.runtime.corpus import Corpus


def test_building_without_an_api_key_fails_loudly(
    bundle: PersonaBundle, monkeypatch: pytest.MonkeyPatch
):
    """The SDK would construct a client fine and raise a bare TypeError on the
    first request — not an APIStatusError, so `_call` would not catch it and the
    caller would get a 500. Boot is the place to find out."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(AgentError, match="ANTHROPIC_API_KEY"):
        Agent.build(bundle, Corpus(Path("/nonexistent")))


def test_a_whitespace_only_api_key_is_no_key(
    bundle: PersonaBundle, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    with pytest.raises(AgentError, match="ANTHROPIC_API_KEY"):
        Agent.build(bundle, Corpus(Path("/nonexistent")))


def test_the_message_never_quotes_the_key(bundle: PersonaBundle, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(AgentError) as caught:
        Agent.build(bundle, Corpus(Path("/nonexistent")))
    assert "sk-" not in str(caught.value)


def test_building_with_a_key_succeeds(bundle: PersonaBundle, monkeypatch: pytest.MonkeyPatch):
    """No network: constructing the client does not call the API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    agent = Agent.build(bundle, Corpus(Path("/nonexistent")))
    assert agent.bundle is bundle
    assert agent.effort == "xhigh"
