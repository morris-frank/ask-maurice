"""Read markdown + YAML frontmatter from the PRIVATE vault.

Build plane only. Nothing under `runtime/` may import this module — see
AGENTS.md § Sensitivity boundary, and `tests/test_boundary.py`, which fails if
that ever stops being true.
"""

from __future__ import annotations

import re
import subprocess  # noqa: S404 - `git rev-parse` on a known local checkout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class Note:
    """One vault markdown file, split into frontmatter and body."""

    path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.path.stem)

    @property
    def aliases(self) -> list[str]:
        raw = self.meta.get("aliases") or []
        values = raw if isinstance(raw, list) else [raw]
        return [str(v).strip() for v in values if str(v).strip()]

    @property
    def emails(self) -> list[str]:
        """Every address on the note: the `emails` key plus email-shaped aliases."""
        raw = self.meta.get("emails") or []
        values = raw if isinstance(raw, list) else [raw]
        candidates = [str(v) for v in values] + self.aliases
        return sorted({c.strip().lower() for c in candidates if "@" in c})


def read_note(path: Path) -> Note:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if match is None:
        return Note(path=path, body=text)
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return Note(path=path, meta=meta, body=match.group(2))


def read_people(vault: Path) -> list[Note]:
    """Every person note, sorted by path so the build is deterministic."""
    notes = [read_note(p) for p in sorted((vault / "people").glob("*.md"))]
    return [n for n in notes if n.meta.get("type") == "person"]


def head_commit(vault: Path) -> str:
    """Short SHA of the private vault, recorded in the bundle for provenance."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
        ["git", "-C", str(vault), "rev-parse", "--short", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"
