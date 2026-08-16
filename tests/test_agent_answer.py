"""The answer pipeline, with a stubbed client. No network, no API key needed.

This file exists because of a real escape: `Agent.answer` was once accidentally
nested inside a module-level function by a bad edit, so `Agent` had no `answer`
attribute at all — and the whole suite stayed green, because nothing called it.
The typechecker caught it and the typechecker is advisory in CI.

So: exercise the method. A test that never calls the thing cannot notice the
thing is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime.agent import Agent, Answer
from ask_maurice.runtime.corpus import Corpus
from ask_maurice.runtime.identity import Caller, from_handle
from ask_maurice.runtime.redaction import Redactor


@dataclass
class _Block:
    type: str
    text: str = ""


@dataclass
class _Response:
    content: list[_Block]
    stop_reason: str = "end_turn"


class _Stream:
    def __init__(self, response: _Response, seen: dict[str, Any]):
        self._response = response
        self._seen = seen

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> _Response:
        return self._response


class _Messages:
    def __init__(self, response: _Response, seen: dict[str, Any]):
        self._response = response
        self._seen = seen

    def stream(self, **kwargs: Any) -> _Stream:
        self._seen.update(kwargs)
        return _Stream(self._response, self._seen)


class _Client:
    """Stands in for `anthropic.Anthropic`, recording the request it was given."""

    def __init__(self, response: _Response):
        self.seen: dict[str, Any] = {}
        self.messages = _Messages(response, self.seen)


def _agent(bundle: PersonaBundle, corpus: Path, response: _Response) -> tuple[Agent, _Client]:
    client = _Client(response)
    agent = Agent(
        bundle=bundle,
        corpus=Corpus(corpus),
        client=client,  # ty: ignore[invalid-argument-type] - structural stand-in
        redactor=Redactor(bundle),
    )
    return agent, client


def test_answer_retrieves_calls_and_cites(bundle: PersonaBundle, shared_vault: Path):
    agent, client = _agent(
        bundle,
        shared_vault,
        _Response([_Block("thinking"), _Block("text", "Because depth varies.")]),
    )
    answer = agent.answer("why do we normalise by sequencing depth?", Caller(handle="anonymous"))

    assert isinstance(answer, Answer)
    assert answer.text == "Because depth varies."
    assert not answer.refused
    assert any(s.startswith("eng/benchmark-normalisation.md@") for s in answer.sources)


def test_answer_sends_the_pinned_model_settings(bundle: PersonaBundle, shared_vault: Path):
    """`budget_tokens`, `temperature`, `top_p`, `top_k` and prefill all 400 on this
    model. If one reappears, this fails rather than production doing so."""
    agent, client = _agent(bundle, shared_vault, _Response([_Block("text", "ok")]))
    agent.answer("sequencing depth", Caller(handle="anonymous"))

    assert client.seen["model"] == "claude-opus-5"
    assert client.seen["thinking"] == {"type": "adaptive"}
    assert client.seen["output_config"] == {"effort": "xhigh"}
    assert not {"budget_tokens", "temperature", "top_p", "top_k"} & set(client.seen)


def test_a_framed_caller_gets_framing_in_the_messages_not_the_system_block(
    bundle: PersonaBundle, shared_vault: Path
):
    """Rule 6: the persona is the cached prefix, framing goes after it."""
    agent, client = _agent(bundle, shared_vault, _Response([_Block("text", "ok")]))
    agent.answer("sequencing depth", from_handle("ada@testco.com", bundle))

    rendered_system = str(client.seen["system"])
    assert "Ada" not in rendered_system
    assert "Ada" in str(client.seen["messages"])


def test_a_refusal_is_reported_as_one_and_does_not_read_the_content(
    bundle: PersonaBundle, shared_vault: Path
):
    agent, _ = _agent(
        bundle, shared_vault, _Response([_Block("text", "junk")], stop_reason="refusal")
    )
    answer = agent.answer("something off", Caller(handle="anonymous"))

    assert answer.refused
    assert "junk" not in answer.text


def test_leaked_persona_content_is_scrubbed_from_the_answer(
    bundle: PersonaBundle, shared_vault: Path
):
    """Last line of defence: the model was told not to recite the persona."""
    leak = bundle.participants["ada-lovelace"].under_pressure
    agent, _ = _agent(bundle, shared_vault, _Response([_Block("text", f"Well, {leak}")]))
    answer = agent.answer("sequencing depth", Caller(handle="anonymous"))

    assert leak not in answer.text


@pytest.mark.parametrize("method", ["answer", "build", "_call"])
def test_the_public_surface_is_still_attached_to_the_class(method: str):
    """The bug this file was written for: a def that silently became nested."""
    assert callable(getattr(Agent, method))
