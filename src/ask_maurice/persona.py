"""The persona bundle: the shared data model both planes agree on.

Pure data, no I/O, no vault access — so the runtime can deserialise a bundle
without importing anything from `build/`. The build plane produces one of these
from the private vault; the runtime plane loads one from Secret Manager and
never learns where it came from.

Treat every string in here as sensitive. `Participant.under_pressure`,
`.dependency_on_maurice` and `.contrast` are candid commentary about people who
can call this agent — they steer the answer, they are never quoted in one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Participant:
    """One colleague, as the persona knows them.

    `slug` is the stable key. `contrast` and `say_yes_to` come from the
    communication-advisor dict, where they are written as coaching *for* Maurice;
    here the doppelgänger is Maurice, so they read as direct instructions.
    """

    slug: str
    display_name: str
    role: str = ""
    rhythm: str = ""
    cares_about: str = ""
    under_pressure: str = ""
    dependency_on_maurice: str = ""
    contrast: str = ""
    say_yes_to: str = ""


@dataclass(frozen=True)
class PersonaBundle:
    schema_version: int
    source_commit: str
    built_from: list[str]
    voice: str
    base_prompt: str
    participants: dict[str, Participant] = field(default_factory=dict)
    # Any handle we might see at runtime -> participant slug. Built at compile
    # time from person-file `aliases`, so the runtime resolves identity by dict
    # lookup and never needs to read a person file itself.
    aliases: dict[str, str] = field(default_factory=dict)

    def resolve(self, handle: str) -> Participant | None:
        slug = self.aliases.get(handle.strip().lower())
        return self.participants.get(slug) if slug else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PersonaBundle:
        version = int(payload.get("schema_version", 0))
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"persona bundle schema v{version}, this build speaks v{SCHEMA_VERSION}"
            )
        participants = {
            slug: Participant(**record) for slug, record in payload.get("participants", {}).items()
        }
        return cls(
            schema_version=version,
            source_commit=str(payload.get("source_commit", "unknown")),
            built_from=list(payload.get("built_from", [])),
            voice=str(payload.get("voice", "")),
            base_prompt=str(payload.get("base_prompt", "")),
            participants=participants,
            aliases={str(k).lower(): str(v) for k, v in payload.get("aliases", {}).items()},
        )
