from __future__ import annotations

import json
from pathlib import Path

import pytest

from ask_maurice.build.compile import (
    BuildError,
    bare_name,
    compile_bundle,
    unjoined,
    write_bundle,
)
from ask_maurice.persona import PersonaBundle


def test_bare_name_strips_the_role_parenthetical():
    assert bare_name("Julia Jehn (Head of Comp Bio)") == "Julia Jehn"
    assert bare_name("Bruno (CEO)") == "Bruno"
    assert bare_name("Someone") == "Someone"


def test_compile_joins_dict_keys_to_person_aliases(private_vault: Path):
    bundle = compile_bundle(private_vault)

    assert set(bundle.participants) == {"ada-lovelace", "grace-hopper", "franziska-boewer"}
    ada = bundle.participants["ada-lovelace"]
    assert ada.display_name == "Ada Lovelace"
    assert ada.contrast == "Lead with the date, not the architecture."

    # The whole point of the build-time join: an email seen at runtime resolves
    # without the runtime ever opening a person file.
    assert bundle.resolve("ada@testco.com") is ada
    assert bundle.resolve("ADA@TESTCO.COM ") is ada
    assert bundle.resolve("ada-testco") is ada
    assert bundle.resolve("nobody@testco.com") is None


def test_umlaut_spelling_still_joins(private_vault: Path):
    """The dict says "Boewer", the person file says "Böwer". Both must resolve."""
    bundle = compile_bundle(private_vault)

    franziska = bundle.participants["franziska-boewer"]
    assert franziska.display_name == "Franziska Böwer"  # the person file spelling wins
    assert bundle.resolve("franziska@testco.com") is franziska
    assert bundle.resolve("Franziska Böwer") is franziska
    assert bundle.resolve("Franziska Boewer") is franziska
    assert not unjoined(bundle)


def test_unjoined_reports_participants_with_no_address(private_vault: Path):
    dict_path = next(private_vault.glob("people/*team prompt dict.json"))
    payload = json.loads(dict_path.read_text())
    payload["participants"]["Nobody Known (Advisor)"] = {"role": "Advisor"}
    dict_path.write_text(json.dumps(payload), encoding="utf-8")

    assert unjoined(compile_bundle(private_vault)) == ["Nobody Known"]


def test_compile_records_provenance(private_vault: Path):
    bundle = compile_bundle(private_vault)
    assert bundle.source_commit != "unknown"
    assert "people/Maurice Frank.md" in bundle.built_from
    assert any(p.endswith("team prompt dict.json") for p in bundle.built_from)
    assert "systems-first" in bundle.voice


def test_compile_keeps_a_participant_with_no_person_note(private_vault: Path, tmp_path: Path):
    dict_path = next(private_vault.glob("people/*team prompt dict.json"))
    payload = json.loads(dict_path.read_text())
    payload["participants"]["Nobody Known (Advisor)"] = {"role": "Advisor", "contrast": "Be brief."}
    dict_path.write_text(json.dumps(payload), encoding="utf-8")

    bundle = compile_bundle(private_vault)
    # Framing survives even though the email join failed.
    unjoined = bundle.resolve("nobody known")
    assert unjoined is not None
    assert unjoined.contrast == "Be brief."


def test_compile_needs_the_subject_note(private_vault: Path):
    (private_vault / "people" / "Maurice Frank.md").unlink()
    with pytest.raises(BuildError, match="subject note"):
        compile_bundle(private_vault)


def test_bundle_roundtrips_and_is_written_private(private_vault: Path, tmp_path: Path):
    bundle = compile_bundle(private_vault)
    path = write_bundle(bundle, tmp_path / "persona" / "bundle.json")

    assert path.stat().st_mode & 0o077 == 0  # not group/world readable
    restored = PersonaBundle.from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert restored == bundle


def test_from_dict_rejects_a_foreign_schema_version(private_vault: Path):
    payload = compile_bundle(private_vault).to_dict()
    payload["schema_version"] = 99
    with pytest.raises(ValueError, match="schema v99"):
        PersonaBundle.from_dict(payload)
