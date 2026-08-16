"""Fusion, provenance across the store boundary, and what the indexer may send."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ask_maurice.config import MixedbreadConfig
from ask_maurice.runtime.corpus import Corpus, Excerpt
from ask_maurice.runtime.mxbai import StoreUnavailable
from ask_maurice.runtime.retrieval import (
    HybridRetriever,
    VaultStore,
    _hash,
    for_config,
    fuse,
    index,
)


def _excerpt(path: str, *, commit: str = "aaaaaaaa", text: str = "text") -> Excerpt:
    return Excerpt(path=path, commit=commit, title=path, text=text, score=1.0)


# --- fusion -----------------------------------------------------------------


def test_fusion_promotes_what_both_backends_liked_over_either_ones_favourite():
    lexical = [_excerpt("a.md"), _excerpt("b.md"), _excerpt("c.md")]
    semantic = [_excerpt("d.md"), _excerpt("b.md"), _excerpt("e.md")]
    assert [e.path for e in fuse(lexical, semantic, limit=3)][0] == "b.md"


def test_fusion_keeps_what_only_one_backend_found():
    """The whole point: BM25 keeps the rare exact token, embeddings keep the paraphrase."""
    fused = fuse([_excerpt("bm25-only.md")], [_excerpt("vector-only.md")], limit=5)
    assert {e.path for e in fused} == {"bm25-only.md", "vector-only.md"}


def test_fusion_prefers_the_local_text_because_its_commit_is_current():
    fused = fuse(
        [_excerpt("a.md", commit="current0", text="from the checkout")],
        [_excerpt("a.md", commit="stale000", text="from the index")],
        limit=1,
    )
    assert fused[0].commit == "current0"
    assert fused[0].text == "from the checkout"


# --- the store as a retriever ------------------------------------------------


class _SearchApi:
    def __init__(self, result: list[dict[str, Any]] | Exception) -> None:
        self.stores = SimpleNamespace(search=self._search)
        self._result = result

    def _search(self, **_: object) -> SimpleNamespace:
        if isinstance(self._result, Exception):
            raise self._result
        return SimpleNamespace(data=[SimpleNamespace(**row) for row in self._result])


def _store(result: list[dict[str, Any]] | Exception) -> VaultStore:
    return VaultStore(api=_SearchApi(result), store="vault")  # ty: ignore[invalid-argument-type]


def test_a_chunk_from_the_store_rebuilds_the_same_citation_a_local_excerpt_carries():
    store = _store(
        [
            {
                "text": "Counts are rarefied before comparison.",
                "filename": "eng__benchmark.md",
                "external_id": "eng/benchmark.md",
                "score": 0.7,
                "metadata": {
                    "path": "eng/benchmark.md",
                    "commit": "deadbeefcafe",
                    "title": "benchmark",
                },
            }
        ]
    )
    (excerpt,) = store.search("depth")
    assert excerpt.cite() == "eng/benchmark.md@deadbeef"


def test_hybrid_falls_back_to_lexical_when_the_store_is_down(shared_vault: Path):
    """A ranking downgrade, not an evidence gap — the notes are on disk either way."""
    corpus = Corpus(root=shared_vault)
    hybrid = HybridRetriever(
        local=corpus, remote=_store(StoreUnavailable("mixedbread is having a day"))
    )
    assert [e.path for e in hybrid.search("sequencing depth")] == [
        e.path for e in corpus.search("sequencing depth")
    ]


def test_local_is_the_default_and_needs_no_mixedbread_at_all(shared_vault: Path):
    corpus = Corpus(root=shared_vault)
    assert for_config(None, corpus) is corpus
    assert (
        for_config(
            MixedbreadConfig(
                api_key="k", literature_store="papers", vault_store="", vault_retrieval="local"
            ),
            corpus,
        )
        is corpus
    )


# --- indexing ----------------------------------------------------------------


class _FakeApi:
    """Enough of the SDK to watch what the indexer sends, and nothing more."""

    def __init__(self, already: dict[str, str] | None = None) -> None:
        self.uploaded: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.blobs: dict[str, tuple[str, bytes, str]] = {}
        self._existing = {
            rel: SimpleNamespace(id=f"file-{i}", external_id=rel, metadata={"sha": sha})
            for i, (rel, sha) in enumerate((already or {}).items())
        }
        self.files = SimpleNamespace(create=self._create_file)
        self.stores = SimpleNamespace(
            files=SimpleNamespace(create=self._attach, list=self._list, delete=self._delete)
        )
        self._next = 1000

    def _create_file(self, *, file: tuple[str, bytes, str]) -> SimpleNamespace:
        self._next += 1
        self.blobs[f"new-{self._next}"] = file
        return SimpleNamespace(id=f"new-{self._next}")

    def _attach(self, store: str, **kwargs: Any) -> SimpleNamespace:
        self.uploaded[kwargs["external_id"]] = kwargs
        return SimpleNamespace(id=kwargs["file_id"])

    def _list(self, store: str, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(
            data=list(self._existing.values()),
            pagination=SimpleNamespace(has_more=False, last_cursor=None),
        )

    def _delete(self, file_id: str, *, store_identifier: str) -> None:
        self.deleted.append(file_id)


def test_the_indexer_sends_exactly_what_retrieval_reads_and_nothing_else(shared_vault: Path):
    """Same filter both ways: no templates, no transcripts, no stray file in the tree."""
    (shared_vault / "persona").mkdir()
    (shared_vault / "persona" / "bundle.json").write_text("{}", encoding="utf-8")
    api = _FakeApi()

    report = index(Corpus(root=shared_vault), api, "vault")  # ty: ignore[invalid-argument-type]

    assert set(api.uploaded) == {
        "eng/benchmark-normalisation.md",
        "lib/provenance.md",
        "org/operating-rhythm.md",
    }
    assert report.uploaded == sorted(api.uploaded)
    assert not any("bundle" in path for path in api.uploaded)


def test_every_uploaded_file_carries_the_path_and_commit_a_citation_needs(shared_vault: Path):
    corpus = Corpus(root=shared_vault)
    api = _FakeApi()

    index(corpus, api, "vault")  # ty: ignore[invalid-argument-type]

    sent = api.uploaded["lib/provenance.md"]
    assert sent["metadata"]["path"] == "lib/provenance.md"
    assert sent["metadata"]["commit"] == corpus.commit
    assert sent["overwrite"] is True


def test_unchanged_notes_are_not_re_embedded(shared_vault: Path):
    corpus = Corpus(root=shared_vault)
    unchanged = {
        rel: _hash((shared_vault / rel).read_text(encoding="utf-8"))
        for rel in ("lib/provenance.md", "org/operating-rhythm.md")
    }
    api = _FakeApi(unchanged)

    report = index(corpus, api, "vault")  # ty: ignore[invalid-argument-type]

    assert set(api.uploaded) == {"eng/benchmark-normalisation.md"}
    assert sorted(report.unchanged) == sorted(unchanged)


def test_a_note_deleted_from_the_vault_leaves_the_index(shared_vault: Path):
    """Otherwise the agent keeps citing a path that no longer exists."""
    api = _FakeApi({"eng/retracted.md": "whatever"})

    report = index(Corpus(root=shared_vault), api, "vault")  # ty: ignore[invalid-argument-type]

    assert report.removed == ["eng/retracted.md"]
    assert api.deleted == ["file-0"]


def test_pruning_can_be_turned_off(shared_vault: Path):
    api = _FakeApi({"eng/retracted.md": "whatever"})
    report = index(
        Corpus(root=shared_vault),
        api,  # ty: ignore[invalid-argument-type]
        "vault",
        prune=False,
    )
    assert report.removed == []
    assert api.deleted == []


@pytest.mark.parametrize("include", [True, False], ids=["opted-in", "default"])
def test_transcripts_reach_the_index_only_when_they_reach_retrieval(
    shared_vault: Path, include: bool
):
    api = _FakeApi()
    corpus = Corpus(root=shared_vault, include_transcripts=include)
    index(corpus, api, "vault")  # ty: ignore[invalid-argument-type]
    assert any(p.startswith("transcripts/") for p in api.uploaded) is include
