"""Synthetic fixtures. No real vault content, no real person, ever."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ask_maurice.persona import SCHEMA_VERSION, Participant, PersonaBundle

DICT = {
    "_meta": {"subject": "Test Subject"},
    "basePrompt": "You advise the subject before internal meetings. Own the platform surface.",
    "participants": {
        "Ada Lovelace (Head of Widgets)": {
            "role": "Head of Widgets; owns the widget line.",
            "rhythm": "Weekly.",
            "caresAbout": "Widget throughput and whether the line holds.",
            "underPressure": "The widget deadline. Wants a date, not options.",
            "dependencyOnMaurice": "The widget dashboard rests on the data foundation.",
            "contrast": "Lead with the date, not the architecture.",
            "sayYesTo": "A committed widget date.",
        },
        # Spelled without the umlaut here and with it in the person file — the
        # real dict does exactly this, and the join has to survive it.
        "Franziska Boewer (Head of Lab)": {
            "role": "Head of Lab.",
            "caresAbout": "Throughput and sample provenance.",
            "contrast": "Lead with what unblocks the bench.",
            "sayYesTo": "A date for the upload path.",
        },
        "Grace Hopper (Ops)": {
            "role": "Operations.",
            "caresAbout": "Process that survives contact with reality.",
            "contrast": "Be concrete about who does what by when.",
            "sayYesTo": "One named owner per step.",
        },
    },
}

PERSON_MD = """\
---
type: person
title: "{title}"
tags: [people]
aliases:
{aliases}
organization: Testco
---

## {title}
{title} does things.
"""

SUBJECT_MD = """\
---
type: person
title: "Maurice Frank"
tags: [people]
aliases:
  - Maurice
---

## Maurice Frank
Detailed, systems-first, precise, hedged. Prefers naming the tradeoff over
smoothing it over.
"""


def _person(title: str, aliases: list[str]) -> str:
    return PERSON_MD.format(title=title, aliases="\n".join(f"  - {a}" for a in aliases))


@pytest.fixture
def private_vault(tmp_path: Path) -> Path:
    """A minimal stand-in for the private vault, with a real git checkout."""
    vault = tmp_path / "vault"
    people = vault / "people"
    people.mkdir(parents=True)
    (people / "Maurice Frank.md").write_text(SUBJECT_MD, encoding="utf-8")
    (people / "Ada Lovelace.md").write_text(
        _person("Ada Lovelace", ["Ada", "ada@testco.com", "ada-testco"]), encoding="utf-8"
    )
    (people / "Grace Hopper.md").write_text(
        _person("Grace Hopper", ["Grace", "grace@testco.com"]), encoding="utf-8"
    )
    (people / "Franziska Böwer.md").write_text(
        _person("Franziska Böwer", ["Franzi", "franziska@testco.com"]), encoding="utf-8"
    )
    (people / "2026-07-22 Subject Advisor - team prompt dict.json").write_text(
        json.dumps(DICT), encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=vault, check=True)
    subprocess.run(["git", "add", "-A"], cwd=vault, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=vault,
        check=True,
    )
    return vault


@pytest.fixture
def shared_vault(tmp_path: Path) -> Path:
    """A stand-in for the SHARED vault: technical dirs, plus transcripts residue."""
    root = tmp_path / "corpus"
    for rel, text in {
        "eng/benchmark-normalisation.md": (
            "# Benchmark normalisation\n\n"
            "Sequencing depth varies per sample, so counts are rarefied before the "
            "benchmark comparison. Compositional data cannot be compared raw.\n\n"
            "The benchmark itself is a percentile against the reference cohort."
        ),
        "lib/provenance.md": (
            "# Provenance\n\nEvery measurement carries the sample id and the protocol "
            "version it was produced under."
        ),
        "org/operating-rhythm.md": "# Operating rhythm\n\nWeekly leads meeting.",
        "transcripts/2026-07-01 call.md": "# Call\n\nWe discussed sequencing depth at length.",
        "templates/note.md": "# Template",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


@pytest.fixture
def bundle() -> PersonaBundle:
    ada = Participant(
        slug="ada-lovelace",
        display_name="Ada Lovelace",
        role="Head of Widgets",
        cares_about="Widget throughput and whether the line holds",
        under_pressure="The widget deadline. Wants a date, not options.",
        dependency_on_maurice="The widget dashboard rests on the data foundation",
        contrast="Lead with the date, not the architecture.",
        say_yes_to="A committed widget date.",
    )
    return PersonaBundle(
        schema_version=SCHEMA_VERSION,
        source_commit="abc1234",
        built_from=["people/Maurice Frank.md"],
        voice="Detailed, systems-first, precise, hedged.",
        base_prompt="You advise the subject before internal meetings.",
        participants={ada.slug: ada},
        aliases={"ada@testco.com": ada.slug, "ada": ada.slug, "ada lovelace": ada.slug},
    )
