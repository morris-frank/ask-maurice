"""The literature tool loop: what the model is told when the lookup fails."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import ToolUseBlock

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime.agent import MAX_TOOL_ROUNDS, Agent
from ask_maurice.runtime.corpus import Excerpt
from ask_maurice.runtime.identity import Caller
from ask_maurice.runtime.literature import Reference
from ask_maurice.runtime.mxbai import StoreUnavailable
from ask_maurice.runtime.prompt import LITERATURE_TOOL
from ask_maurice.runtime.redaction import Redactor

EXCERPT = Excerpt(
    path="eng/benchmark.md",
    commit="abc1234def",
    title="benchmark",
    text="Counts are rarefied before comparison.",
    score=2.0,
)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_call(query: str = "tillage rhizosphere", call_id: str = "tu_1") -> ToolUseBlock:
    return ToolUseBlock(id=call_id, input={"query": query}, name=LITERATURE_TOOL, type="tool_use")


class _FakeClient:
    """Replays a scripted list of responses and records every request."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        response = self._responses.pop(0) if self._responses else _final("ran out of script")

        class _Stream:
            def __enter__(self_inner) -> Any:
                return self_inner

            def __exit__(self_inner, *_: object) -> bool:
                return False

            def get_final_message(self_inner) -> SimpleNamespace:
                return response

        return _Stream()


def _final(text: str) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", content=[_text(text)])


def _wants_tool(*calls: ToolUseBlock) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="tool_use", content=list(calls))


class _Retriever:
    def search(self, query: str, limit: int = 6) -> list[Excerpt]:
        return [EXCERPT]


class _Literature:
    def __init__(self, result: list[Reference] | Exception) -> None:
        self.result = result
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 6) -> list[Reference]:
        self.queries.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _agent(bundle: PersonaBundle, client: _FakeClient, literature: object | None) -> Agent:
    return Agent(
        bundle=bundle,
        retriever=_Retriever(),
        client=client,  # ty: ignore[invalid-argument-type]
        redactor=Redactor(bundle),
        literature=literature,  # ty: ignore[invalid-argument-type]
    )


def test_no_literature_configured_means_no_tool_is_offered(bundle: PersonaBundle):
    client = _FakeClient([_final("answered from the vault")])
    answer = _agent(bundle, client, None).answer("why rarefy?", Caller(handle="ada"))

    assert client.requests[0]["tools"] == []
    assert answer.references == []
    assert answer.sources == ["eng/benchmark.md@abc1234d"]


def test_a_paper_is_reported_separately_from_a_vault_path(bundle: PersonaBundle):
    """Different warrants: one is Maurice's own decision, the other is evidence."""
    reference = Reference(citation="Smith, 2021, Tillage", text="…", score=0.8, doi="10.1/x")
    client = _FakeClient([_wants_tool(_tool_call()), _final("what the paper found was…")])

    answer = _agent(bundle, client, _Literature([reference])).answer("q", Caller(handle="ada"))

    assert answer.sources == ["eng/benchmark.md@abc1234d"]
    assert answer.references == ["Smith, 2021, Tillage (doi:10.1/x)"]


def test_an_outage_reaches_the_model_as_an_error_not_as_an_empty_result(bundle: PersonaBundle):
    """The stub's whole warning: the model must be able to tell these apart."""
    client = _FakeClient([_wants_tool(_tool_call()), _final("I couldn't check the literature.")])

    answer = _agent(bundle, client, _Literature(StoreUnavailable("down"))).answer(
        "q", Caller(handle="ada")
    )

    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "could not be reached" in tool_result["content"]
    assert answer.references == []


def test_an_empty_collection_is_not_reported_as_an_error(bundle: PersonaBundle):
    client = _FakeClient([_wants_tool(_tool_call()), _final("we have no paper on that")])

    _agent(bundle, client, _Literature([])).answer("q", Caller(handle="ada"))

    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert "is_error" not in tool_result
    assert "gap in the collection" in tool_result["content"]


def test_the_assistant_turn_is_replayed_verbatim_so_thinking_blocks_survive(
    bundle: PersonaBundle,
):
    call = _tool_call()
    client = _FakeClient([_wants_tool(call), _final("done")])

    _agent(bundle, client, _Literature([])).answer("q", Caller(handle="ada"))

    replayed = client.requests[1]["messages"][-2]
    assert replayed["role"] == "assistant"
    assert replayed["content"] == [call]


def test_a_model_that_keeps_searching_still_gets_to_answer(bundle: PersonaBundle):
    """Out of rounds, tools withdrawn, one final pass — never a dead request."""
    literature = _Literature([])
    client = _FakeClient(
        [_wants_tool(_tool_call(call_id=f"tu_{i}")) for i in range(MAX_TOOL_ROUNDS)]
        + [_final("here is what I have")]
    )

    answer = _agent(bundle, client, literature).answer("q", Caller(handle="ada"))

    assert answer.text == "here is what I have"
    assert len(literature.queries) == MAX_TOOL_ROUNDS
    assert client.requests[-1]["tools"] == []


@pytest.mark.parametrize("bad", [{}, {"query": ""}, {"query": 7}], ids=["missing", "empty", "int"])
def test_a_malformed_tool_call_is_answered_with_an_error_not_a_crash(
    bundle: PersonaBundle, bad: dict[str, Any]
):
    call = ToolUseBlock(id="tu_1", input=bad, name=LITERATURE_TOOL, type="tool_use")
    client = _FakeClient([_wants_tool(call), _final("fine")])

    _agent(bundle, client, _Literature([])).answer("q", Caller(handle="ada"))

    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_a_refusal_short_circuits_before_any_tool_runs(bundle: PersonaBundle):
    literature = _Literature([])
    client = _FakeClient([SimpleNamespace(stop_reason="refusal", content=[])])

    answer = _agent(bundle, client, literature).answer("q", Caller(handle="ada"))

    assert answer.refused
    assert literature.queries == []
