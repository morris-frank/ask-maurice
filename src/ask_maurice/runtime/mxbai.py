"""Shared plumbing for the mixedbread stores, and nothing store-specific.

One client, one search call, one error type. Both the literature store and the
optional shared-vault store go through here, which is the point: "a unified
RAG-style interface" is only unified if the two callers cannot drift apart in
how they retrieve, how they time out, or how they fail.

Failure is the load-bearing part. A retrieval backend that returns an empty list
when it is actually unreachable teaches the model that the literature is silent
on the question, and the model will then answer as if the literature agreed with
it. Every failure here raises `StoreUnavailable` instead, so the caller can say
"I could not check" — a different sentence from "there is nothing".

Timeouts are short and retries few on purpose. This sits inside a single
question-answering request; a store that is slow is, for this purpose, a store
that is down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mixedbread

# One question should not wait on a slow index. Two retries inside that budget
# absorbs a blip; anything longer is better reported than waited out.
TIMEOUT_SECONDS = 20.0
MAX_RETRIES = 2


class StoreUnavailable(RuntimeError):
    """The store could not be reached or refused the query. Never 'no results'."""


@dataclass(frozen=True)
class Passage:
    """One scored chunk, flattened out of the SDK's union of chunk types.

    `external_id` is how provenance survives the round trip: whatever the
    indexer set as the file's external id comes back unchanged, so a vault
    chunk can be traced to a repo path and a literature chunk to its source
    document without trusting the model to carry the reference.
    """

    store: str
    filename: str
    external_id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


def client(api_key: str) -> mixedbread.Mixedbread:
    return mixedbread.Mixedbread(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES)


def _as_dict(value: object) -> dict[str, Any]:
    """File metadata is typed `object` by the SDK; only a mapping is usable."""
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def search(
    api: mixedbread.Mixedbread,
    store: str,
    query: str,
    *,
    top_k: int = 6,
    rerank: bool = True,
) -> list[Passage]:
    """Search one store. Raises `StoreUnavailable`; never returns None."""
    try:
        response = api.stores.search(
            store_identifiers=[store],
            query=query,
            top_k=top_k,
            search_options={"return_metadata": True, "rerank": rerank},
        )
    except mixedbread.NotFoundError as exc:
        raise StoreUnavailable(f"mixedbread store {store!r} does not exist") from exc
    except mixedbread.AuthenticationError as exc:
        raise StoreUnavailable("mixedbread rejected the API key") from exc
    except mixedbread.RateLimitError as exc:
        raise StoreUnavailable("mixedbread rate limited the request") from exc
    except mixedbread.APIStatusError as exc:
        raise StoreUnavailable(f"mixedbread returned {exc.status_code}") from exc
    except mixedbread.APIConnectionError as exc:
        raise StoreUnavailable("could not reach mixedbread") from exc

    passages: list[Passage] = []
    for chunk in response.data:
        # Image, audio and video chunks share the response union and carry no
        # text. Nothing downstream can quote them, so they are dropped here
        # rather than surfacing as empty excerpts.
        text = (getattr(chunk, "text", None) or "").strip()
        if not text:
            continue
        passages.append(
            Passage(
                store=store,
                filename=getattr(chunk, "filename", "") or "",
                external_id=getattr(chunk, "external_id", None) or "",
                text=text,
                score=float(getattr(chunk, "score", 0.0)),
                metadata=_as_dict(getattr(chunk, "metadata", None)),
            )
        )
    return passages
