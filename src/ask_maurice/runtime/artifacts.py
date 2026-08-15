"""Decide whether a question deserves an artifact, even when nobody asked.

The rule this implements: an explainer question that feeds off the technical
docs is usually worth more than a chat reply. Someone asking "how does the
benchmark normalisation actually work" is asking for a thing they can re-read,
forward, or hand to the next person — a document, a podcast, or an explainer
video.

This module only *proposes*. It runs cheap lexical heuristics over the question
and over which parts of the vault the retrieval actually hit, and hands the
model a candidate plus its reasoning. The model makes the final call, because
the heuristics cannot tell a genuine explainer from a rhetorical one.

Honesty about what exists: only `DOCUMENT` can be produced today. `PODCAST` and
`EXPLAINER_VIDEO` are routed and offered, not generated — NotebookLM is not
wired up. `Suggestion.available` carries that distinction into the prompt so the
agent offers them as a next step rather than pretending to deliver one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ask_maurice.runtime.corpus import Excerpt

# Where the technical docs live on the shared vault.
TECHNICAL_DIRS = frozenset({"eng", "lib", "svc", "v3", "eco"})

_EXPLAINER = re.compile(
    r"\b(explain|explainer|how does|how do we|how did we|why do we|why did we|what is|"
    r"what are|walk me through|talk me through|overview of|onboard|primer|"
    r"introduction to|background on|rationale|catch me up)\b",
    re.IGNORECASE,
)
_EXPLICIT_DOC = re.compile(
    r"\b(write|draft|document|doc|one[- ]pager|write[- ]?up|memo|summary|brief)\b", re.IGNORECASE
)
_SHAREABLE = re.compile(
    r"\b((for|to) the team|for everyone|share|onboarding|new joiner|all[- ]hands|"
    r"before the meeting|listen)\b",
    re.IGNORECASE,
)
_VISUAL = re.compile(r"\b(show|diagram|visual|walkthrough|demo|screencast|video)\b", re.IGNORECASE)
_LOOKUP = re.compile(
    r"\b(who|when|where|which file|what'?s the|status of|did we|do we have|is there|link to)\b",
    re.IGNORECASE,
)


class ArtifactKind(StrEnum):
    NONE = "none"
    DOCUMENT = "document"
    PODCAST = "podcast"
    EXPLAINER_VIDEO = "explainer-video"


@dataclass(frozen=True)
class Suggestion:
    kind: ArtifactKind
    reason: str
    available: bool

    def as_instruction(self) -> str:
        if self.kind is ArtifactKind.NONE:
            return (
                "No artifact is warranted here — answer directly. Only offer one if the "
                "answer turns out to be longer or more reusable than this question implied."
            )
        if self.available:
            return (
                f"This question is a candidate for a written {self.kind.value} ({self.reason}). "
                "If the answer runs long or is something they will want to re-read or forward, "
                "produce it as a structured document with headings, and cite the vault paths it "
                "draws on. If a short answer genuinely suffices, say so and offer the document."
            )
        return (
            f"This question is a candidate for a {self.kind.value} ({self.reason}). That format "
            "is not wired up yet, so answer directly and offer it as a next step — say plainly "
            "that it would need to be produced separately. Never imply one has been made."
        )


def _technical_share(excerpts: list[Excerpt]) -> float:
    if not excerpts:
        return 0.0
    hits = sum(1 for e in excerpts if e.path.split("/", 1)[0] in TECHNICAL_DIRS)
    return hits / len(excerpts)


def classify(question: str, excerpts: list[Excerpt] | None = None) -> Suggestion:
    excerpts = excerpts or []
    technical = _technical_share(excerpts)

    if _EXPLICIT_DOC.search(question):
        return Suggestion(ArtifactKind.DOCUMENT, "they asked for one", available=True)

    explainer = bool(_EXPLAINER.search(question))
    if not explainer:
        return Suggestion(ArtifactKind.NONE, "not an explainer question", available=False)

    # An explainer question that didn't land in the technical docs is usually a
    # process or people question — answer it, don't manufacture a deliverable.
    if technical < 0.5:
        return Suggestion(
            ArtifactKind.NONE, "explainer, but not grounded in the technical docs", available=False
        )

    if _VISUAL.search(question):
        return Suggestion(
            ArtifactKind.EXPLAINER_VIDEO, "an explainer they asked to be shown", available=False
        )
    if _SHAREABLE.search(question):
        return Suggestion(
            ArtifactKind.PODCAST, "an explainer meant for others to consume async", available=False
        )
    if _LOOKUP.search(question) and len(excerpts) <= 2:
        return Suggestion(ArtifactKind.NONE, "narrow lookup, one source", available=False)
    return Suggestion(
        ArtifactKind.DOCUMENT, "an explainer drawing on the technical docs", available=True
    )
