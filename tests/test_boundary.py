"""The sensitivity boundary, asserted in code.

AGENTS.md says the runtime never touches the private vault. That is only true as
long as nobody adds an import. These tests fail if someone does.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

import pytest

from ask_maurice.config import BuildConfig, RuntimeConfig

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "src" / "ask_maurice" / "runtime"
PRIVATE_VAULT_ENV = "ASK_MAURICE_PRIVATE_VAULT"
# The only module allowed to send anything to a third-party store.
UPLOADER = "retrieval.py"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", sorted(RUNTIME_DIR.glob("*.py")), ids=lambda p: p.name)
def test_runtime_never_imports_the_build_plane(module: Path):
    offenders = {n for n in _imported_modules(module) if n.startswith("ask_maurice.build")}
    assert not offenders, (
        f"{module.name} imports {sorted(offenders)}. The build plane reads .kbignore'd "
        "vault paths; nothing hosted may reach it. See AGENTS.md § Sensitivity boundary."
    )


@pytest.mark.parametrize("module", sorted(RUNTIME_DIR.glob("*.py")), ids=lambda p: p.name)
def test_runtime_never_names_the_private_vault_variable(module: Path):
    source = module.read_text(encoding="utf-8")
    # Allowed in a comment explaining the rule; never as a string the code reads.
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert PRIVATE_VAULT_ENV not in code, f"{module.name} reads {PRIVATE_VAULT_ENV}"


def test_runtime_config_has_no_field_pointing_at_private_content():
    assert "private_vault" in {f.name for f in fields(BuildConfig)}
    assert "private_vault" not in {f.name for f in fields(RuntimeConfig)}


def test_the_module_that_uploads_cannot_see_the_persona_bundle():
    """Indexing sends content to mixedbread. The bundle must not be reachable there.

    Not a style rule: it is the difference between "the indexer does not upload
    the bundle today" and "the indexer cannot be made to upload the bundle
    without an import that fails this test first".
    """
    offenders = {
        name
        for name in _imported_modules(RUNTIME_DIR / UPLOADER)
        if name.startswith("ask_maurice.persona") or name.endswith(".bundle")
    }
    assert not offenders, f"{UPLOADER} imports {sorted(offenders)}; it must not reach the bundle"


@pytest.mark.parametrize(
    "module",
    [p for p in sorted(RUNTIME_DIR.glob("*.py")) if p.name != UPLOADER],
    ids=lambda p: p.name,
)
def test_nothing_but_the_indexer_sends_content_to_a_store(module: Path):
    code = "\n".join(
        line.split("#", 1)[0] for line in module.read_text(encoding="utf-8").split("\n")
    )
    assert "files.create" not in code, (
        f"{module.name} uploads to a third-party store. Keep every write path in {UPLOADER}, "
        "where what may be sent is bounded by Corpus.documents()."
    )
