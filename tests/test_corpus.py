from __future__ import annotations

from pathlib import Path

from ask_maurice.runtime.corpus import Corpus


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


def test_search_returns_nothing_for_an_unrelated_question(shared_vault: Path):
    assert Corpus(shared_vault).search("kubernetes ingress certificates") == []


def test_stopwords_alone_match_nothing(shared_vault: Path):
    assert Corpus(shared_vault).search("what is the") == []
