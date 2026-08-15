"""Pre-commit guard: refuse to commit a compiled persona bundle.

The bundle is built from .kbignore'd vault paths and contains candid per-person
commentary about people who can call this agent. .gitignore already excludes it;
this hook is the second lock, so a `git add -f` can't quietly land one.

Matches by path shape and by content signature, because a bundle renamed to
something innocuous is exactly the case .gitignore misses.
"""

import json
import sys
from pathlib import Path

# PersonaBundle's serialized field names — see src/ask_maurice/persona.py.
BUNDLE_KEYS = {"schema_version", "base_prompt", "participants"}


def looks_like_bundle(path: Path) -> bool:
    if path.name.startswith("persona-bundle") or "persona/" in path.as_posix():
        return True
    if path.suffix != ".json":
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and BUNDLE_KEYS.issubset(payload.keys())


def main(argv: list[str]) -> int:
    offenders = [p for arg in argv if looks_like_bundle(p := Path(arg))]
    if not offenders:
        return 0
    print("refusing to commit compiled persona bundle(s):", file=sys.stderr)
    for path in offenders:
        print(f"  {path}", file=sys.stderr)
    print(
        "\nThe bundle travels build plane -> Secret Manager -> memory, never git.\n"
        "See AGENTS.md § Sensitivity boundary.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
