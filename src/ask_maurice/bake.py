"""Bake the retrievable subset of a corpus checkout into a directory for an image.

Neither plane. This runs on the machine that builds the container, reads a local
shared-vault checkout, and writes something `COPY` can take. It imports the
runtime's `Corpus` so the baked set is *by construction* the same set retrieval
would read — a second implementation of the skip rules would drift, and the drift
would show up as an answer that cites a note the image does not contain.

Why bake at all, rather than `COPY corpus/`: measured 2026-08-16, the checkout is
2.7 GB (1.2 GB `.git`, and a working tree that is mostly `lib/` binaries), while
what `Corpus.documents()` returns is 564 markdown files totalling 22.6 MB. The
`.git` directory is the single biggest item and it is also the one the runtime
does not need — provided the commit travels some other way, which is what the
`COMMIT` file beside the documents is for.

Nothing here touches the private vault or the persona bundle. The corpus is the
shared vault: the same content any Soilytix employee can already clone.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ask_maurice.runtime.corpus import COMMIT_FILE, Corpus, CorpusError


class BakeError(RuntimeError):
    """The corpus could not be baked into the output directory."""


@dataclass(frozen=True)
class BakeResult:
    out: Path
    commit: str
    documents: int
    bytes_copied: int


def _clear(out: Path) -> None:
    """Make `out` an empty directory, refusing anything that is not ours to delete.

    A previous bake is identified by its `COMMIT` file. Without that marker the
    directory could be anything — someone's `dist/` with a wheel in it, or a typo
    pointing at a real tree — and wiping it is not this command's business.
    """
    if not out.exists():
        return
    if not out.is_dir():
        raise BakeError(f"{out} exists and is not a directory")
    if not any(out.iterdir()):
        return
    if not (out / COMMIT_FILE).is_file():
        raise BakeError(
            f"{out} is not empty and carries no {COMMIT_FILE} file, so it is not a previous "
            "bake. Point --out somewhere else, or remove it yourself."
        )
    shutil.rmtree(out)


def bake(corpus: Corpus, out: Path) -> BakeResult:
    """Copy every document retrieval would read into `out`, plus the commit SHA."""
    # Before the copy: an unreadable commit is a broken image, and finding that
    # out after writing 22 MB is worse than finding it out now.
    commit = corpus.commit
    documents = corpus.documents()
    if not documents:
        raise BakeError(
            f"{corpus.root} yielded no retrievable documents — refusing to bake an empty corpus"
        )

    _clear(out)
    out.mkdir(parents=True, exist_ok=True)

    copied = 0
    for path in documents:
        target = out / path.relative_to(corpus.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied += target.stat().st_size

    (out / COMMIT_FILE).write_text(f"{commit}\n", encoding="utf-8")
    return BakeResult(out=out, commit=commit, documents=len(documents), bytes_copied=copied)


def bake_from(root: Path, out: Path, *, include_transcripts: bool = False) -> BakeResult:
    """`bake`, given a checkout path rather than a `Corpus`."""
    if not root.is_dir():
        raise BakeError(f"no corpus checkout at {root} — run `ask-maurice corpus-sync` first")
    try:
        return bake(Corpus(root=root, include_transcripts=include_transcripts), out)
    except CorpusError as exc:
        raise BakeError(str(exc)) from None
