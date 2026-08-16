from __future__ import annotations

from pathlib import Path

import pytest

from ask_maurice.runtime.corpus import Corpus, CorpusError


def test_templates_and_transcripts_are_excluded_by_default(shared_vault: Path):
    paths = {p.relative_to(shared_vault).as_posix() for p in Corpus(shared_vault).documents()}
    assert "eng/benchmark-normalisation.md" in paths
    assert "templates/note.md" not in paths
    assert "transcripts/2026-07-01 call.md" not in paths


def test_transcripts_are_opt_in(shared_vault: Path):
    corpus = Corpus(shared_vault, include_transcripts=True)
    paths = {p.relative_to(shared_vault).as_posix() for p in corpus.documents()}
    assert "transcripts/2026-07-01 call.md" in paths


def test_search_ranks_the_right_note_and_carries_provenance(shared_vault: Path):
    hits = Corpus(shared_vault).search("how does sequencing depth normalisation work?")
    assert hits
    assert hits[0].path == "eng/benchmark-normalisation.md"
    assert hits[0].cite().startswith("eng/benchmark-normalisation.md@")
    assert "rarefied" in hits[0].text


def test_a_rare_term_outranks_repeated_filler(chatty_vault: Path):
    """The in-toto failure: four common words drowning out the one that matters."""
    hits = Corpus(chatty_vault).search("why would we use something like in-toto?")
    assert hits[0].path == "eng/attestations.md"


def test_search_returns_nothing_for_an_unrelated_question(shared_vault: Path):
    assert Corpus(shared_vault).search("kubernetes ingress certificates") == []


def test_stopwords_alone_match_nothing(shared_vault: Path):
    assert Corpus(shared_vault).search("what is the") == []


# --- provenance: a git checkout in dev, a COMMIT file in the image -----------


def test_a_baked_corpus_serves_without_git(baked_vault: Path):
    corpus = Corpus(baked_vault)
    assert not (baked_vault / ".git").exists()
    paths = {p.relative_to(baked_vault).as_posix() for p in corpus.documents()}
    assert paths == {"eng/benchmark-normalisation.md", "lib/provenance.md"}


def test_a_baked_corpus_cites_the_commit_from_the_commit_file(baked_vault: Path):
    hits = Corpus(baked_vault).search("sequencing depth normalisation")
    assert hits
    assert hits[0].cite() == "eng/benchmark-normalisation.md@0f1e2d3c"


def test_git_wins_over_a_stray_commit_file(shared_vault: Path):
    """A checkout that also has a COMMIT file still reports its real HEAD."""
    (shared_vault / "COMMIT").write_text("deadbeefdeadbeef\n", encoding="utf-8")
    assert Corpus(shared_vault).commit != "deadbeefdeadbeef"


def test_no_provenance_at_all_is_refused(tmp_path: Path):
    """The failure this guard exists for: documents with no SHA to cite them at."""
    root = tmp_path / "orphan"
    (root / "eng").mkdir(parents=True)
    (root / "eng" / "note.md").write_text("# Note\n\nSequencing depth.", encoding="utf-8")
    corpus = Corpus(root)
    with pytest.raises(CorpusError, match="no provenance"):
        corpus.documents()
    with pytest.raises(CorpusError, match="no provenance"):
        _ = corpus.commit


def test_an_empty_commit_file_is_not_provenance(tmp_path: Path):
    """A blank COMMIT would otherwise cite every excerpt at `@` and look fine."""
    root = tmp_path / "blank"
    (root / "eng").mkdir(parents=True)
    (root / "eng" / "note.md").write_text("# Note\n\nSequencing depth.", encoding="utf-8")
    (root / "COMMIT").write_text("   \n", encoding="utf-8")
    with pytest.raises(CorpusError, match="no provenance"):
        Corpus(root).documents()
