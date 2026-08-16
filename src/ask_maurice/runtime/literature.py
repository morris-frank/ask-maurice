"""Science-literature lookup over the mixedbread papers store.

The corpus is the research collection assembled from Norman's Zotero library and
uploaded to mixedbread. It is *third-party evidence*, which is what makes it a
separate path from the vault rather than more rows in the same index: a vault
excerpt is Maurice's own decision and he can assert it in the first person, and
a paper is somebody else's finding that has to be attributed and can disagree
with him. Collapsing the two would let the agent launder a claim from a paper
into a Soilytix position, which is the exact move the science rules forbid.

The v1 stub warned that a half-wired literature path is worse than none, because
the agent cannot tell "no result" from "not connected". That distinction is now
carried in the types: an outage raises `StoreUnavailable` out of `mxbai.search`,
`agent.py` turns it into a tool error the model is told to report, and an empty
result list means the collection genuinely had nothing.

This is retrieval, not a literature review. It returns passages with their source
document, and the agent is still required to say when a claim needs a source the
collection does not hold.
"""

from __future__ import annotations

from dataclasses import dataclass

import mixedbread

from ask_maurice.config import MixedbreadConfig
from ask_maurice.runtime.mxbai import Passage, StoreUnavailable, search

DEFAULT_LIMIT = 6

# Zotero's own field names, which is what an export-driven upload will carry if
# the file metadata was set at ingest. Absent that, the filename is the citation
# — degraded, but honest, and never invented.
_TITLE_KEYS = ("title", "Title", "publicationTitle")
_AUTHOR_KEYS = ("authors", "author", "creators", "firstCreator")
_YEAR_KEYS = ("year", "date", "issued")
_DOI_KEYS = ("doi", "DOI")


@dataclass(frozen=True)
class Reference:
    """A retrieved passage plus whatever is known about where it came from."""

    citation: str
    text: str
    score: float
    doi: str = ""

    def cite(self) -> str:
        return f"{self.citation} (doi:{self.doi})" if self.doi else self.citation


def _first(metadata: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list) and value:
            return ", ".join(str(v) for v in value[:3])
    return ""


def _reference(passage: Passage) -> Reference:
    title = _first(passage.metadata, _TITLE_KEYS) or passage.filename or passage.external_id
    author = _first(passage.metadata, _AUTHOR_KEYS)
    year = _first(passage.metadata, _YEAR_KEYS)[:4]
    citation = ", ".join(part for part in (author, year, title) if part)
    return Reference(
        citation=citation or "unattributed source",
        text=passage.text,
        score=passage.score,
        doi=_first(passage.metadata, _DOI_KEYS),
    )


@dataclass(frozen=True)
class Literature:
    """The papers store, or nothing. Constructed once per process."""

    api: mixedbread.Mixedbread
    store: str

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[Reference]:
        """Raises `StoreUnavailable` — the caller must not read that as empty."""
        return [_reference(p) for p in search(self.api, self.store, query, top_k=limit)]


def from_config(config: MixedbreadConfig | None) -> Literature | None:
    """None when no literature store is configured; the agent then offers no tool."""
    if config is None or not config.literature_enabled:
        return None
    from ask_maurice.runtime.mxbai import client

    return Literature(api=client(config.api_key), store=config.literature_store)


def status(literature: Literature | None) -> str:
    if literature is None:
        return "literature lookup is not configured; answers are vault-only."
    return f"literature lookup is live against mixedbread store {literature.store!r}."


__all__ = ["DEFAULT_LIMIT", "Literature", "Reference", "StoreUnavailable", "from_config", "status"]
