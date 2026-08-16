"""Where vault excerpts come from: the local BM25 scan, mixedbread, or both.

`corpus.py` argues for lexical retrieval and the argument still holds — 577
markdown files scan in milliseconds and every excerpt carries an exact
`path@commit`. What it cannot do is match a question to a note that says the
same thing in different words, which is most of how people actually ask
("does the depth thing bias the cohort comparison?" against a note that only
ever writes "rarefaction" and "reference cohort").

So this module adds a semantic index rather than replacing the lexical one, and
`hybrid` is the mode worth running. The two backends fail in opposite
directions: BM25 is the one that finds `in-toto` in the twenty-two notes that
name it, embeddings are the one that finds the note that never uses the asker's
word. Reciprocal rank fusion merges them without needing their scores to be
comparable, which they are not.

Provenance is preserved across the boundary rather than re-derived. The indexer
writes the repo-relative path, the commit it indexed, and a content hash into
each file's metadata, so a chunk that comes back from mixedbread reconstructs
the same `path@commit` citation a local excerpt carries. The commit on a
mixedbread hit is the commit *as of indexing*, which is why a hybrid hit prefers
the local excerpt: same document, but text read from the checkout that is on
disk right now.

What may be indexed: only what `Corpus.documents()` yields. That is the
shared-vault checkout, minus `templates/`, minus `transcripts/` unless
explicitly opted in. The private vault has no path into this module, and the
persona bundle is not a document — it never becomes store content.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

import mixedbread

from ask_maurice.config import MixedbreadConfig
from ask_maurice.runtime.corpus import Corpus, Excerpt
from ask_maurice.runtime.mxbai import Passage, StoreUnavailable, client, search

log = logging.getLogger(__name__)

# Standard reciprocal-rank-fusion constant. Large enough that the gap between
# rank 1 and rank 2 does not swamp a document that both backends ranked mid-list
# — which is exactly the document fusion exists to promote.
RRF_K = 60

MARKDOWN_MIME = "text/markdown"


class Retriever(Protocol):
    """What `agent.py` needs. `Corpus` already satisfies it; so do the others."""

    def search(self, query: str, limit: int = ...) -> list[Excerpt]: ...


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _excerpt(passage: Passage) -> Excerpt:
    """Rebuild a vault citation from the metadata the indexer wrote."""
    path = str(passage.metadata.get("path") or passage.external_id or passage.filename)
    return Excerpt(
        path=path,
        commit=str(passage.metadata.get("commit") or ""),
        title=str(passage.metadata.get("title") or path.rsplit("/", 1)[-1].removesuffix(".md")),
        text=passage.text,
        score=passage.score,
    )


@dataclass(frozen=True)
class VaultStore:
    """Semantic retrieval over the indexed shared vault."""

    api: mixedbread.Mixedbread
    store: str

    def search(self, query: str, limit: int = 6) -> list[Excerpt]:
        """Raises `StoreUnavailable`; `HybridRetriever` is what absorbs that."""
        return [_excerpt(p) for p in search(self.api, self.store, query, top_k=limit)]


@dataclass(frozen=True)
class HybridRetriever:
    """BM25 and embeddings, fused by rank.

    Degrades to the local corpus when the store is unreachable, and says so in
    the log. That is a ranking downgrade, not an evidence gap — the same notes
    are on disk either way — which is why it is allowed here and forbidden for
    the literature path, where there is no local copy to fall back to.
    """

    local: Corpus
    remote: VaultStore

    def search(self, query: str, limit: int = 6) -> list[Excerpt]:
        lexical = self.local.search(query, limit=limit)
        try:
            semantic = self.remote.search(query, limit=limit)
        except StoreUnavailable as exc:
            log.warning("vault store unavailable, answering on lexical retrieval only: %s", exc)
            return lexical
        return fuse(lexical, semantic, limit=limit)


def fuse(lexical: list[Excerpt], semantic: list[Excerpt], *, limit: int) -> list[Excerpt]:
    """Reciprocal rank fusion, keyed by path.

    The lexical excerpt wins a tie on text because it was read from the checkout
    on disk, so its commit is current rather than as-of-index.
    """
    scores: dict[str, float] = {}
    chosen: dict[str, Excerpt] = {}
    for ranking in (semantic, lexical):
        for rank, excerpt in enumerate(ranking):
            scores[excerpt.path] = scores.get(excerpt.path, 0.0) + 1.0 / (RRF_K + rank + 1)
            chosen[excerpt.path] = excerpt
    ordered = sorted(scores, key=lambda path: scores[path], reverse=True)
    return [
        Excerpt(
            path=path,
            commit=chosen[path].commit,
            title=chosen[path].title,
            text=chosen[path].text,
            score=scores[path],
        )
        for path in ordered[:limit]
    ]


def for_config(config: MixedbreadConfig | None, corpus: Corpus) -> Retriever:
    """The retriever the configuration asks for. Local unless told otherwise."""
    if config is None or not config.vault_store_enabled:
        return corpus
    remote = VaultStore(api=client(config.api_key), store=config.vault_store)
    if config.vault_retrieval == "mixedbread":
        return remote
    return HybridRetriever(local=corpus, remote=remote)


# --- indexing ---------------------------------------------------------------


@dataclass
class IndexReport:
    commit: str
    uploaded: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.uploaded) + len(self.unchanged)


@dataclass(frozen=True)
class _Indexed:
    """What the store already holds for one path: its file id and content hash."""

    file_id: str
    sha: str


def _existing(api: mixedbread.Mixedbread, store: str) -> dict[str, _Indexed]:
    """external_id -> what is already indexed, for every file in the store."""
    known: dict[str, _Indexed] = {}
    cursor: str | None = None
    while True:
        page = api.stores.files.list(store, limit=100, **({"after": cursor} if cursor else {}))
        for entry in page.data:
            if not entry.external_id:
                continue
            meta = entry.metadata if isinstance(entry.metadata, dict) else {}
            known[entry.external_id] = _Indexed(entry.id, str(meta.get("sha", "")))
        if not page.pagination.has_more or not page.pagination.last_cursor:
            return known
        cursor = page.pagination.last_cursor


def _documents(corpus: Corpus) -> Iterator[tuple[str, str]]:
    for path in corpus.documents():
        try:
            yield path.relative_to(corpus.root).as_posix(), path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            log.warning("skipping unreadable file %s", path.name)


def index(
    corpus: Corpus,
    api: mixedbread.Mixedbread,
    store: str,
    *,
    prune: bool = True,
    on_file: Callable[[str], None] | None = None,
) -> IndexReport:
    """Push the shared-vault checkout into `store`, skipping unchanged files.

    Idempotent by construction: the repo-relative path is the file's external id
    and re-uploads overwrite in place, so running this after every `corpus-sync`
    converges rather than accumulating duplicates. Unchanged files are compared
    by content hash and skipped, because re-embedding 577 notes to change three
    of them is the kind of cost that quietly makes a nightly job expensive.
    """
    report = IndexReport(commit=corpus.commit)
    known = _existing(api, store)
    seen: set[str] = set()

    for rel, text in _documents(corpus):
        seen.add(rel)
        sha = _hash(text)
        indexed = known.get(rel)
        if indexed is not None and indexed.sha == sha:
            report.unchanged.append(rel)
            continue
        if on_file:
            on_file(rel)
        # Slashes are not safe in a filename; the path lives in `external_id`
        # and in the metadata, which is what citations are rebuilt from anyway.
        uploaded = api.files.create(
            file=(rel.replace("/", "__"), text.encode("utf-8"), MARKDOWN_MIME)
        )
        api.stores.files.create(
            store,
            file_id=uploaded.id,
            external_id=rel,
            overwrite=True,
            metadata={
                "path": rel,
                "commit": report.commit,
                "title": rel.rsplit("/", 1)[-1].removesuffix(".md"),
                "section": rel.split("/", 1)[0],
                "sha": sha,
            },
        )
        report.uploaded.append(rel)

    if prune:
        # A note deleted from the vault has to leave the index too, or the agent
        # keeps citing a path that no longer exists at a commit that no longer
        # has it — a retracted decision answered as current.
        for stale in sorted(set(known) - seen):
            api.stores.files.delete(known[stale].file_id, store_identifier=store)
            report.removed.append(stale)
    return report
