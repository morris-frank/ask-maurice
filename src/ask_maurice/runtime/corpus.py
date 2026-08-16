"""The retrieval corpus: a clone of the SHARED vault, and nothing else.

`ASK_MAURICE_CORPUS_REMOTE` points at `Soilytix/vault` — the same content any
Soilytix employee can already clone. This module is the only thing the agent
reads to answer a question, which is what makes "the agent can only answer from
the shared vault" a property of the code rather than a promise in a prompt.

Retrieval is deliberately plain lexical scoring, no embeddings, no index server.
577 markdown files is small; a term-frequency scan over them takes milliseconds
and, unlike a vector store, its provenance is exact — every excerpt carries the
path and the commit it came from, which is what a science answer needs anyway.

Scoring is BM25, and the two parts of it are both load-bearing here. Without the
IDF term, "why would we use something so unknown like in-toto?" scores `use`,
`something` and `like` as heavily as `in-toto`, so the four filler words decide
the ranking and every one of the 22 notes that actually name the tool falls
outside the top 30. Without length normalisation, long meeting transcripts win
on word count alone against the specs that answer the question.
"""

from __future__ import annotations

import math
import re
import subprocess  # noqa: S404 - fixed argv git calls against a known checkout
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Machinery and templates, not knowledge.
SKIP_DIRS = {".git", ".obsidian", ".bin", "templates"}
# Pre-rule residue: these graduated 2026-07-28, `transcripts/` entered .kbignore
# 2026-07-30. Shared, but never a deliberate share decision — opt in explicitly.
TRANSCRIPTS_DIR = "transcripts"

# Provenance for a corpus with no `.git`. The container image bakes only the 564
# markdown files retrieval actually reads (22.6 MB) rather than the 2.7 GB
# checkout they live in, so the commit has to travel beside them in a plain file.
COMMIT_FILE = "COMMIT"

# Standard BM25 constants: term-frequency saturation and length normalisation.
_K1, _B = 1.2, 0.75

_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*")
_STOP = frozenset(
    "a an and are as at be but by for from has have how in into is it its of on or that the "
    "their there these this to was were what when where which who why will with you your do "
    "does did we our us can could should would".split()
)


class CorpusError(RuntimeError):
    """The corpus is missing, stale beyond use, or not a git checkout."""


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2]


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],  # noqa: S607
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise CorpusError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@dataclass(frozen=True)
class Excerpt:
    """A retrieved passage with the provenance an answer must be able to cite."""

    path: str
    commit: str
    title: str
    text: str
    score: float

    def cite(self) -> str:
        return f"{self.path}@{self.commit[:8]}"


@dataclass
class Corpus:
    root: Path
    include_transcripts: bool = False

    @property
    def commit(self) -> str:
        """The SHA every `Excerpt.cite()` hangs off. Never guessed, never blank."""
        if (self.root / ".git").exists():
            return _git("rev-parse", "HEAD", cwd=self.root)
        if baked := self._baked_commit():
            return baked
        raise CorpusError(self._no_provenance())

    def _baked_commit(self) -> str:
        path = self.root / COMMIT_FILE
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def _no_provenance(self) -> str:
        return (
            f"{self.root} has no provenance: neither a .git checkout nor a non-empty "
            f"{COMMIT_FILE} file. Run `ask-maurice corpus-sync` for a local clone, or "
            "`ask-maurice bake-corpus` to produce the baked corpus an image ships."
        )

    def documents(self) -> list[Path]:
        # A baked corpus has no `.git`, and that is fine — but *some* provenance is
        # mandatory. Serving answers whose citations point at an unknown commit is
        # worse than serving none, so refuse rather than degrade.
        if not (self.root / ".git").exists() and not self._baked_commit():
            raise CorpusError(self._no_provenance())
        out = []
        for path in sorted(self.root.rglob("*.md")):
            parts = path.relative_to(self.root).parts
            if SKIP_DIRS.intersection(parts):
                continue
            if parts[0] == TRANSCRIPTS_DIR and not self.include_transcripts:
                continue
            out.append(path)
        return out

    def search(self, query: str, limit: int = 6) -> list[Excerpt]:
        terms = Counter(tokenize(query))
        if not terms:
            return []
        scanned = []
        for path in self.documents():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = path.relative_to(self.root).as_posix()
            tokens = tokenize(text)
            # Path and filename carry real signal in this vault — `eng/`, `lib/`,
            # `svc/` and dated titles are how the notes are organised.
            scanned.append((path, rel, text, Counter(tokens), Counter(tokenize(rel)), len(tokens)))
        if not scanned:
            return []

        n = len(scanned)
        doc_freq: Counter[str] = Counter()
        for *_, counts, path_counts, _ in scanned:
            doc_freq.update(set(counts) | set(path_counts))
        avg_len = sum(length for *_, length in scanned) / n

        commit = self.commit
        scored: list[Excerpt] = []
        for path, rel, text, counts, path_counts, length in scanned:
            score = 0.0
            for term, weight in terms.items():
                freq = counts.get(term, 0) + 3 * path_counts.get(term, 0)
                if not freq:
                    continue
                # +1 inside the log keeps IDF positive for a term in every note,
                # so a common word can never subtract from a document's score.
                idf = math.log(1 + (n - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                saturated = freq * (_K1 + 1) / (freq + _K1 * (1 - _B + _B * length / avg_len))
                score += weight * idf * saturated
            if score <= 0:
                continue
            scored.append(
                Excerpt(
                    path=rel,
                    commit=commit,
                    title=path.stem,
                    text=_best_window(text, set(terms)),
                    score=score,
                )
            )
        scored.sort(key=lambda e: e.score, reverse=True)
        return scored[:limit]


def _best_window(text: str, terms: set[str], width: int = 1200) -> str:
    """The densest `width`-char window, snapped to paragraph boundaries."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return text[:width]
    best_start, best_hits = 0, -1
    for start in range(len(paragraphs)):
        window, size = [], 0
        for para in paragraphs[start:]:
            if size + len(para) > width and window:
                break
            window.append(para)
            size += len(para)
        hits = sum(1 for w in tokenize("\n".join(window)) if w in terms)
        if hits > best_hits:
            best_start, best_hits = start, hits
    window, size = [], 0
    for para in paragraphs[best_start:]:
        if size + len(para) > width and window:
            break
        window.append(para)
        size += len(para)
    return "\n\n".join(window)


def sync(root: Path, remote: str, ref: str) -> str:
    """Clone or fast-forward the shared-vault checkout. Returns the new HEAD."""
    if not (root / ".git").exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--depth", "1", "--branch", ref, remote, str(root))
    else:
        _git("fetch", "--depth", "1", "origin", ref, cwd=root)
        _git("reset", "--hard", f"origin/{ref}", cwd=root)
        _git("clean", "-fd", cwd=root)
    return _git("rev-parse", "HEAD", cwd=root)
