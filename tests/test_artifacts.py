from __future__ import annotations

from ask_maurice.runtime.artifacts import ArtifactKind, classify
from ask_maurice.runtime.corpus import Excerpt


def excerpts(*paths: str) -> list[Excerpt]:
    return [Excerpt(path=p, commit="abc1234", title=p, text="…", score=1.0) for p in paths]


TECHNICAL = excerpts("eng/benchmark-normalisation.md", "lib/provenance.md")
NON_TECHNICAL = excerpts("org/operating-rhythm.md", "com/positioning.md")


def test_explicit_request_wins():
    s = classify("write me a one-pager on the benchmark", TECHNICAL)
    assert s.kind is ArtifactKind.DOCUMENT
    assert s.available


def test_technical_explainer_proposes_a_document_unprompted():
    s = classify("how does the benchmark normalisation actually work?", TECHNICAL)
    assert s.kind is ArtifactKind.DOCUMENT
    assert s.available


def test_explainer_that_missed_the_technical_docs_gets_no_artifact():
    s = classify("how do we run the weekly leads meeting?", NON_TECHNICAL)
    assert s.kind is ArtifactKind.NONE


def test_plain_lookup_gets_no_artifact():
    assert classify("who owns the report generator?", TECHNICAL).kind is ArtifactKind.NONE


def test_shareable_explainer_routes_to_podcast_and_admits_it_is_not_built():
    s = classify("can you explain the normalisation for the team to listen to?", TECHNICAL)
    assert s.kind is ArtifactKind.PODCAST
    assert not s.available
    assert "not wired up" in s.as_instruction()
    assert "Never imply one has been made" in s.as_instruction()


def test_visual_explainer_routes_to_video():
    s = classify("walk me through the pipeline with a diagram", TECHNICAL)
    assert s.kind is ArtifactKind.EXPLAINER_VIDEO
    assert not s.available


def test_no_retrieval_hits_means_no_artifact():
    assert classify("explain the benchmark", []).kind is ArtifactKind.NONE
