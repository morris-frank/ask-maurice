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
