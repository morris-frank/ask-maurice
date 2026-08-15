"""The model call, and the pipeline around it.

One request per question: retrieve from the shared vault, decide whether an
artifact is warranted, frame for the caller, ask Claude.

Model settings are not free choices. On `claude-opus-5` thinking is adaptive and
on by default; `budget_tokens`, `temperature`, `top_p`, `top_k` and assistant
prefill are all 400s on this model, so none of them appear here and none of them
should be added back. Effort starts at `xhigh` because the work is synthesis
across several retrieved notes in a specific register — sweep it down if latency
matters more than the answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import anthropic

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime import prompt as prompt_mod
from ask_maurice.runtime.artifacts import Suggestion, classify
from ask_maurice.runtime.corpus import Corpus, Excerpt
from ask_maurice.runtime.identity import Caller
from ask_maurice.runtime.redaction import Redactor

MODEL = "claude-opus-5"
MAX_TOKENS = 8192

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


@dataclass
class Agent:
    bundle: PersonaBundle
    corpus: Corpus
    client: anthropic.Anthropic
    redactor: Redactor
    effort: Effort = DEFAULT_EFFORT

    @classmethod
    def build(cls, bundle: PersonaBundle, corpus: Corpus, effort: Effort = DEFAULT_EFFORT) -> Agent:
        return cls(
            bundle=bundle,
            corpus=corpus,
            client=anthropic.Anthropic(),
            redactor=Redactor(bundle),
            effort=effort,
        )

    def answer(self, question: str, caller: Caller) -> Answer:
        excerpts = self.corpus.search(question)
        suggestion = classify(question, excerpts)
        text, refused = self._call(question, caller, excerpts, suggestion)

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
        )

    def _call(
        self, question: str, caller: Caller, excerpts: list[Excerpt], suggestion: Suggestion
    ) -> tuple[str, bool]:
        try:
            with self.client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                system=prompt_mod.system_blocks(self.bundle),
                messages=prompt_mod.messages(question, caller, excerpts, suggestion),
            ) as stream:
                response = stream.get_final_message()
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

        # Checked before touching `.content`: on a refusal the content blocks are
        # not the answer, and reading them as one produces nonsense.
        if response.stop_reason == "refusal":
            return (
                "I can't answer that one. If it was a reasonable question phrased "
                "awkwardly, try it again more directly.",
                True,
            )

        # getattr, not `block.text`: the content list also holds thinking and
        # tool blocks, which have no `.text`.
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", "") == "text"
        )
        return text.strip(), False
