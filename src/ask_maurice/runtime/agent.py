"""The model call, and the pipeline around it.

Vault retrieval happens before the call and literature retrieval happens during
it, and the asymmetry is deliberate. The vault is the ground the persona stands
on — every question is answered from it, so it is retrieved unconditionally and
costs a millisecond-scale local scan. The literature is a paid, latency-bearing
lookup that most questions ("what did we decide about X") have no use for, so
the model asks for it when the answer actually turns on an external finding.

Model settings are not free choices. On `claude-opus-5` thinking is adaptive and
on by default; `budget_tokens`, `temperature`, `top_p`, `top_k` and assistant
prefill are all 400s on this model, so none of them appear here and none of them
should be added back. Effort starts at `xhigh` because the work is synthesis
across several retrieved notes in a specific register — sweep it down if latency
matters more than the answer.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
from anthropic.types import MessageParam, ToolUseBlock

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime import prompt as prompt_mod
from ask_maurice.runtime.artifacts import Suggestion, classify
from ask_maurice.runtime.corpus import Excerpt
from ask_maurice.runtime.identity import Caller
from ask_maurice.runtime.literature import Literature, Reference
from ask_maurice.runtime.mxbai import StoreUnavailable
from ask_maurice.runtime.redaction import Redactor
from ask_maurice.runtime.retrieval import Retriever

MODEL = "claude-opus-5"
MAX_TOKENS = 8192

# A ceiling on tool rounds, not a target. Two searches is a rephrase after a
# thin first hit; past that the model is trawling, and the answer is better
# served by saying the collection does not cover it.
MAX_TOOL_ROUNDS = 3

Effort = Literal["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT: Effort = "xhigh"

log = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """The answer could not be produced. Never carries prompt or bundle content."""


@dataclass(frozen=True)
class Answer:
    text: str
    caller: str
    suggestion: Suggestion
    sources: list[str]
    refused: bool = False
    # Kept apart from `sources` all the way out to the caller: a vault path is
    # something Maurice wrote, a paper is somebody else's evidence, and the two
    # do not belong in one undifferentiated citation list.
    references: list[str] = field(default_factory=list)


@dataclass
class Agent:
    bundle: PersonaBundle
    retriever: Retriever
    client: anthropic.Anthropic
    redactor: Redactor
    literature: Literature | None = None
    effort: Effort = DEFAULT_EFFORT

    @classmethod
    def build(
        cls,
        bundle: PersonaBundle,
        retriever: Retriever,
        literature: Literature | None = None,
        effort: Effort = DEFAULT_EFFORT,
    ) -> Agent:
        _require_api_key()
        return cls(
            bundle=bundle,
            retriever=retriever,
            client=anthropic.Anthropic(),
            redactor=Redactor(bundle),
            literature=literature,
            effort=effort,
        )

    def answer(self, question: str, caller: Caller) -> Answer:
        excerpts = self.retriever.search(question)
        suggestion = classify(question, excerpts)
        text, refused, references = self._call(question, caller, excerpts, suggestion)

        # Last line of defence: the model was told not to recite the persona, and
        # this checks that it didn't. A hit is a bug worth seeing in the logs —
        # the caller just gets the answer without the leaked span.
        if not refused and self.redactor.leaks(text):
            log.warning("persona content detected in answer to %s; scrubbed", caller.handle)
            text = self.redactor.scrub(text)

        return Answer(
            text=text,
            caller=caller.handle,
            suggestion=suggestion,
            sources=[e.cite() for e in excerpts],
            refused=refused,
            references=[r.cite() for r in references],
        )

    def _call(
        self, question: str, caller: Caller, excerpts: list[Excerpt], suggestion: Suggestion
    ) -> tuple[str, bool, list[Reference]]:
        available = self.literature is not None
        system = prompt_mod.system_blocks(self.bundle, literature=available)
        tools = prompt_mod.tools(literature=available)
        history: list[MessageParam] = list(
            prompt_mod.messages(question, caller, excerpts, suggestion)
        )
        cited: list[Reference] = []

        for _ in range(MAX_TOOL_ROUNDS):
            response = self._request(system, tools, history)

            # Checked before touching `.content`: on a refusal the content blocks
            # are not the answer, and reading them as one produces nonsense.
            if response.stop_reason == "refusal":
                return (
                    "I can't answer that one. If it was a reasonable question phrased "
                    "awkwardly, try it again more directly.",
                    True,
                    [],
                )
            if response.stop_reason != "tool_use":
                return _text_of(response), False, cited

            # The assistant turn goes back verbatim, thinking blocks included —
            # dropping them breaks the signature check on the next request.
            history.append({"role": "assistant", "content": response.content})
            results, found = self._run_tools(response)
            cited.extend(found)
            history.append({"role": "user", "content": results})

        # Out of rounds mid-search. One more pass with no tools, so the model
        # answers from what it has instead of the request dying on the loop
        # bound and the caller getting nothing.
        response = self._request(system, [], history)
        return _text_of(response), False, cited

    def _request(
        self, system: list[Any], tools: list[Any], history: list[MessageParam]
    ) -> anthropic.types.Message:
        try:
            with self.client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                system=system,
                tools=tools,
                messages=history,
            ) as stream:
                return stream.get_final_message()
        # Most specific first: a bare `except anthropic.APIStatusError` ahead of
        # these would swallow both.
        except anthropic.NotFoundError as exc:
            raise AgentError(f"model {MODEL} is not available to this key") from exc
        except anthropic.RateLimitError as exc:
            raise AgentError("rate limited by the Anthropic API; retry shortly") from exc
        except anthropic.APIStatusError as exc:
            raise AgentError(f"Anthropic API returned {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError("could not reach the Anthropic API") from exc

    def _run_tools(self, response: anthropic.types.Message) -> tuple[list[Any], list[Reference]]:
        """Execute every tool call in one assistant turn. Never raises."""
        results: list[Any] = []
        found: list[Reference] = []
        for block in response.content:
            # isinstance, not a `.type` check: it is what narrows the content
            # union, and `.id` does not exist on most of its members.
            if not isinstance(block, ToolUseBlock):
                continue
            if block.name != prompt_mod.LITERATURE_TOOL or self.literature is None:
                results.append(_tool_error(block.id, f"no tool named {block.name!r}"))
                continue
            query = (block.input or {}).get("query", "") if isinstance(block.input, dict) else ""
            if not isinstance(query, str) or not query.strip():
                results.append(_tool_error(block.id, "query must be a non-empty string"))
                continue
            try:
                references = self.literature.search(query)
            except StoreUnavailable as exc:
                # Reported as a tool error, not as an empty result. The prompt
                # requires the model to say it could not check — it can only do
                # that if the failure reaches it as a failure.
                log.warning("literature lookup failed: %s", exc)
                results.append(
                    _tool_error(
                        block.id,
                        "the literature collection could not be reached for this query",
                    )
                )
                continue
            found.extend(references)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": prompt_mod.literature_result(references),
                }
            )
        return results, found


def _tool_error(tool_use_id: str, message: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": message,
        "is_error": True,
    }


def _text_of(response: anthropic.types.Message) -> str:
    # getattr, not `block.text`: the content list also holds thinking and tool
    # blocks, which have no `.text`.
    return "".join(
        getattr(block, "text", "")
        for block in response.content
        if getattr(block, "type", "") == "text"
    ).strip()


def _require_api_key() -> None:
    """Refuse to build an agent that cannot call the model.

    Without a key the SDK constructs a client quite happily and only fails on the
    first request — with a plain `TypeError`, which is not an `APIStatusError`
    and so falls straight past `_call`'s handlers into a 500. The container would
    boot, pass `/healthz`, and answer its first real question with a stack trace
    that looks like a model problem rather than a missing deploy variable.

    `create_app` calls `Agent.build` before uvicorn binds, so checking here turns
    that into a container that refuses to start — the same bargain the corpus
    guard makes in the Dockerfile.
    """
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise AgentError(
            "ANTHROPIC_API_KEY is not set, so no question can be answered. Set it in "
            ".env.local for a local run, or wire it into the Cloud Run deployment "
            "(Secret Manager) alongside ASK_MAURICE_BUNDLE_SECRET."
        )
