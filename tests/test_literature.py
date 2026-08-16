"""Citations: built from what the store actually knows, never invented."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ask_maurice.config import MixedbreadConfig
from ask_maurice.runtime.literature import Literature, from_config
from ask_maurice.runtime.mxbai import Passage


def _passage(metadata: dict[str, Any] | None = None) -> Passage:
    return Passage(
        store="papers",
        filename="smith-2021-rhizosphere.pdf",
        external_id="zotero/ABCD1234",
        text="Rhizosphere community composition shifted with tillage.",
        score=0.8,
        metadata=metadata or {},
    )


class _Api:
    def __init__(self, passages: list[Passage]) -> None:
        self._passages = passages
        self.stores = SimpleNamespace(search=self._search)

    def _search(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    text=p.text,
                    filename=p.filename,
                    external_id=p.external_id,
                    score=p.score,
                    metadata=p.metadata,
                )
                for p in self._passages
            ]
        )


def _literature(passages: list[Passage]) -> Literature:
    return Literature(api=_Api(passages), store="papers")  # ty: ignore[invalid-argument-type]


def test_zotero_metadata_becomes_a_real_citation():
    lit = _literature(
        [
            _passage(
                {
                    "authors": ["Smith, J.", "Norman, R.", "Vega, P.", "Ito, K."],
                    "date": "2021-06-01",
                    "title": "Tillage and rhizosphere community structure",
                    "doi": "10.1234/soil.2021.42",
                }
            )
        ]
    )
    (reference,) = lit.search("tillage effect on rhizosphere")
    assert reference.citation == (
        "Smith, J., Norman, R., Vega, P., 2021, Tillage and rhizosphere community structure"
    )
    assert reference.cite().endswith("(doi:10.1234/soil.2021.42)")


def test_bare_upload_degrades_to_the_filename_rather_than_making_one_up():
    """Most of the collection is PDFs with no metadata set. That is honest, not broken."""
    (reference,) = _literature([_passage()]).search("q")
    assert reference.citation == "smith-2021-rhizosphere.pdf"
    assert reference.doi == ""
    assert "doi" not in reference.cite()


def test_empty_collection_result_is_an_empty_list_not_an_error():
    assert _literature([]).search("something nobody has written about") == []


def test_no_store_configured_means_no_literature_at_all():
    """The agent then offers no tool, rather than a tool that silently finds nothing."""
    assert from_config(None) is None
    assert (
        from_config(
            MixedbreadConfig(
                api_key="k", literature_store="", vault_store="", vault_retrieval="local"
            )
        )
        is None
    )
