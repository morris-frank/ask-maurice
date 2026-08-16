from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from ask_maurice.bake import BakeError, bake_from
from ask_maurice.runtime.corpus import Corpus

BAKE_MODULE = Path(__file__).resolve().parents[1] / "src" / "ask_maurice" / "bake.py"


def test_baking_never_reaches_the_build_plane():
    """`bake-corpus` runs on the image-build machine, where the private vault is
    also on disk. It reads the SHARED checkout and nothing else — same rule as
    runtime/, asserted here because bake.py lives outside runtime/'s glob."""
    tree = ast.parse(BAKE_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {n for n in imported if n.startswith("ask_maurice.build")}

    code = "\n".join(
        line.split("#", 1)[0] for line in BAKE_MODULE.read_text(encoding="utf-8").splitlines()
    )
    assert "ASK_MAURICE_PRIVATE_VAULT" not in code


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_bake_copies_only_what_retrieval_reads(shared_vault: Path, tmp_path: Path):
    out = tmp_path / "dist"
    result = bake_from(shared_vault, out)

    baked = {p.relative_to(out).as_posix() for p in out.rglob("*.md")}
    assert baked == {
        "eng/benchmark-normalisation.md",
        "lib/provenance.md",
        "org/operating-rhythm.md",
    }
    # The two exclusions retrieval already makes, and the .git it does not read.
    assert not (out / "templates").exists()
    assert not (out / "transcripts").exists()
    assert not (out / ".git").exists()
    assert result.documents == 3


def test_bake_writes_the_checkouts_head_beside_the_documents(shared_vault: Path, tmp_path: Path):
    out = tmp_path / "dist"
    result = bake_from(shared_vault, out)

    assert result.commit == _head(shared_vault)
    assert (out / "COMMIT").read_text(encoding="utf-8").strip() == result.commit


def test_the_baked_corpus_answers_the_same_question_as_the_checkout(
    shared_vault: Path, tmp_path: Path
):
    """The point of the whole exercise: same documents, same commit, same citation."""
    out = tmp_path / "dist"
    bake_from(shared_vault, out)

    source = Corpus(shared_vault).search("how does sequencing depth normalisation work?")
    baked = Corpus(out).search("how does sequencing depth normalisation work?")
    assert [e.cite() for e in baked] == [e.cite() for e in source]


def test_transcripts_bake_in_only_when_retrieval_would_read_them(
    shared_vault: Path, tmp_path: Path
):
    out = tmp_path / "dist"
    bake_from(shared_vault, out, include_transcripts=True)
    assert (out / "transcripts" / "2026-07-01 call.md").is_file()


def test_rebaking_over_a_previous_bake_replaces_it(shared_vault: Path, tmp_path: Path):
    out = tmp_path / "dist"
    bake_from(shared_vault, out)
    stale = out / "eng" / "since-deleted.md"
    stale.write_text("# Gone\n", encoding="utf-8")

    bake_from(shared_vault, out)
    assert not stale.exists()


def test_bake_refuses_to_clobber_a_directory_that_is_not_a_bake(shared_vault: Path, tmp_path: Path):
    out = tmp_path / "dist"
    out.mkdir()
    (out / "something-precious.whl").write_text("not ours", encoding="utf-8")

    with pytest.raises(BakeError, match="not a previous bake"):
        bake_from(shared_vault, out)
    assert (out / "something-precious.whl").is_file()


def test_bake_refuses_a_checkout_with_no_provenance(tmp_path: Path):
    root = tmp_path / "orphan"
    (root / "eng").mkdir(parents=True)
    (root / "eng" / "note.md").write_text("# Note\n", encoding="utf-8")

    with pytest.raises(BakeError, match="no provenance"):
        bake_from(root, tmp_path / "dist")


def test_bake_refuses_a_missing_checkout(tmp_path: Path):
    with pytest.raises(BakeError, match="no corpus checkout"):
        bake_from(tmp_path / "nope", tmp_path / "dist")


def test_bake_refuses_an_empty_corpus(tmp_path: Path):
    """An empty bake makes a green build that answers every question with nothing."""
    root = _empty_checkout(tmp_path / "empty")
    with pytest.raises(BakeError, match="no retrievable documents"):
        bake_from(root, tmp_path / "dist")


def _empty_checkout(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "README.txt").write_text("no markdown here", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root
