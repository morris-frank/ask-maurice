"""Compile the persona bundle from the private vault.

Three inputs, all under `.kbignore`'d paths and therefore build-plane-only:

  people/Maurice Frank.md          -> the voice (body prose, not frontmatter)
  people/*team prompt dict.json    -> base prompt + per-participant framing
  <optional style notes>           -> extra voice material, opt-in via env

The join that makes asker-awareness work happens here, not at runtime: each key
in the advisor dict ("Julia Jehn (Head of Comp Bio)") is matched to a person
note, and that note's `aliases` — which already carry corporate email addresses
— become the runtime lookup table. No new mapping table, and the runtime never
opens a person file.

A dict key that matches no person note is kept as a participant anyway, keyed by
its own slug, with only the name as an alias. Losing the framing entirely would
be worse than losing the email join.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import replace
from pathlib import Path

from ask_maurice.build.vault import Note, head_commit, read_note, read_people
from ask_maurice.persona import SCHEMA_VERSION, Participant, PersonaBundle

SUBJECT_NOTE = "people/Maurice Frank.md"
DICT_GLOB = "people/*team prompt dict.json"

# "Julia Jehn (Head of Comp Bio)" -> "Julia Jehn"
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")

_FIELD_MAP = {
    "role": "role",
    "rhythm": "rhythm",
    "caresAbout": "cares_about",
    "underPressure": "under_pressure",
    "dependencyOnMaurice": "dependency_on_maurice",
    "contrast": "contrast",
    "sayYesTo": "say_yes_to",
}


class BuildError(RuntimeError):
    """A required build input is missing or malformed."""


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def fold(name: str) -> set[str]:
    """Comparable forms of a name, because umlauts are spelled two ways.

    The advisor dict says "Franziska Boewer"; her person file says "Franziska
    Böwer". Both are correct German. Accent-stripping alone gives "Bower" and
    transliteration alone gives "Boewer", so produce both and match on either —
    otherwise the join silently fails and she gets no framing when she asks.
    """
    lowered = name.strip().lower()
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", lowered) if not unicodedata.combining(c)
    )
    return {lowered, stripped, lowered.translate(_UMLAUTS)}


def slugify(name: str) -> str:
    """Umlauts transliterate (ö -> oe) before any remaining accents are stripped.

    Order matters: NFKD first would turn "Böwer" into "bower", which is neither
    spelling anyone uses.
    """
    transliterated = name.strip().lower().translate(_UMLAUTS)
    ascii_name = "".join(
        c for c in unicodedata.normalize("NFKD", transliterated) if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


def bare_name(dict_key: str) -> str:
    return _PARENTHETICAL.sub("", dict_key).strip()


def style_note_paths(vault: Path) -> list[Path]:
    """Extra voice material, opt-in.

    Deliberately not a glob over `me/` — that directory holds notes *about* other
    people's writing styles as well as Maurice's own, and compiling someone
    else's register into his persona would be a quiet, hard-to-spot error. The
    paths live in the environment rather than in this repo so private filenames
    stay out of git.
    """
    raw = os.environ.get("ASK_MAURICE_STYLE_NOTES", "").strip()
    if not raw:
        return []
    paths = []
    for part in raw.split(":"):
        if not part.strip():
            continue
        path = vault / part.strip()
        if not path.is_file():
            raise BuildError(f"style note not found: {path}")
        paths.append(path)
    return paths


def find_dict(vault: Path) -> Path:
    matches = sorted(vault.glob(DICT_GLOB))
    if not matches:
        raise BuildError(f"no communication-advisor dict under {vault / DICT_GLOB}")
    return matches[-1]  # filenames are date-prefixed; newest wins


def match_person(name: str, people: list[Note]) -> Note | None:
    needles = fold(name)
    for note in people:
        candidates = fold(note.title) | {f for alias in note.aliases for f in fold(alias)}
        if needles & candidates:
            return note
    return None


def _participant(
    dict_key: str, record: dict[str, object], people: list[Note]
) -> tuple[Participant, list[str]]:
    name = bare_name(dict_key)
    note = match_person(name, people)
    display = note.title if note else name
    slug = slugify(display)
    fields = {attr: str(record.get(key, "") or "").strip() for key, attr in _FIELD_MAP.items()}
    participant = Participant(slug=slug, display_name=display, **fields)
    handles = [slug, *fold(name)]
    if note is not None:
        handles += list(fold(note.title))
        handles += [f for alias in note.aliases for f in fold(alias)] + note.emails
    return participant, handles


def unjoined(bundle: PersonaBundle) -> list[str]:
    """Participants with no email alias — i.e. the dict-key -> person join failed.

    Worth surfacing rather than swallowing: an unjoined participant still gets
    framing when addressed by name, but never when they ask over Slack or Entra,
    because those channels only ever produce an address.
    """
    joined = {slug for handle, slug in bundle.aliases.items() if "@" in handle}
    return sorted(p.display_name for slug, p in bundle.participants.items() if slug not in joined)


def compile_bundle(vault: Path) -> PersonaBundle:
    subject = vault / SUBJECT_NOTE
    if not subject.is_file():
        raise BuildError(f"subject note not found: {subject}")

    dict_path = find_dict(vault)
    payload = json.loads(dict_path.read_text(encoding="utf-8"))
    base_prompt = str(payload.get("basePrompt", "")).strip()
    if not base_prompt:
        raise BuildError(f"{dict_path.name} has no basePrompt")

    people = read_people(vault)
    participants: dict[str, Participant] = {}
    aliases: dict[str, str] = {}
    for dict_key, record in (payload.get("participants") or {}).items():
        if not isinstance(record, dict):
            continue
        participant, handles = _participant(dict_key, record, people)
        # A slug collision means two dict keys resolved to one person; merge
        # rather than silently dropping the second record's fields.
        if participant.slug in participants:
            existing = participants[participant.slug]
            participant = replace(
                participant,
                **{
                    attr: getattr(existing, attr) or getattr(participant, attr)
                    for attr in _FIELD_MAP.values()
                },
            )
        participants[participant.slug] = participant
        for handle in handles:
            aliases.setdefault(handle, participant.slug)

    style_paths = style_note_paths(vault)
    voice_parts = [read_note(subject).body.strip()]
    voice_parts += [read_note(p).body.strip() for p in style_paths]

    built_from = [SUBJECT_NOTE, dict_path.relative_to(vault).as_posix()]
    built_from += [p.relative_to(vault).as_posix() for p in style_paths]

    return PersonaBundle(
        schema_version=SCHEMA_VERSION,
        source_commit=head_commit(vault),
        built_from=built_from,
        voice="\n\n---\n\n".join(part for part in voice_parts if part),
        base_prompt=base_prompt,
        participants=participants,
        aliases=aliases,
    )


def write_bundle(bundle: PersonaBundle, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)  # sensitive asset; not world-readable even on a laptop
    return path
