"""The one thing this layer must never do: report an outage as an empty result."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import mixedbread
import pytest

from ask_maurice.runtime.mxbai import StoreUnavailable, search


class _Stores:
    def __init__(self, response: object | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _Api:
    def __init__(self, response: object | Exception) -> None:
        self.stores = _Stores(response)


def _chunk(**kwargs: object) -> SimpleNamespace:
    base = {
        "text": "rarefaction before comparison",
        "filename": "paper.pdf",
        "external_id": "eng/note.md",
        "score": 0.9,
        "metadata": {"path": "eng/note.md"},
    }
    return SimpleNamespace(**(base | kwargs))


def _status_error(status: int) -> mixedbread.APIStatusError:
    request = httpx.Request("POST", "https://api.mixedbread.com/v1/stores/search")
    return mixedbread.APIStatusError(
        "boom", response=httpx.Response(status, request=request), body=None
    )


def test_passages_carry_the_metadata_provenance_depends_on():
    api = _Api(SimpleNamespace(data=[_chunk()]))
    (passage,) = search(api, "vault", "depth normalisation")  # ty: ignore[invalid-argument-type]
    assert passage.external_id == "eng/note.md"
    assert passage.metadata == {"path": "eng/note.md"}
    assert passage.score == pytest.approx(0.9)


def test_chunks_without_text_are_dropped_not_returned_empty():
    """Image and audio chunks share the response union and cannot be quoted."""
    api = _Api(SimpleNamespace(data=[_chunk(text=None), _chunk(text="   "), _chunk()]))
    assert len(search(api, "vault", "q")) == 1  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "error",
    [
        _status_error(404),
        _status_error(401),
        _status_error(429),
        _status_error(500),
        mixedbread.APIConnectionError(request=httpx.Request("POST", "https://api.mixedbread.com")),
    ],
    ids=["not-found", "auth", "rate-limit", "server", "connection"],
)
def test_every_failure_raises_rather_than_returning_nothing(error: Exception):
    """An empty list means the collection had nothing. Nothing else may mean it."""
    with pytest.raises(StoreUnavailable):
        search(_Api(error), "vault", "q")  # ty: ignore[invalid-argument-type]
