from __future__ import annotations

from ask_maurice.persona import Participant, PersonaBundle
from ask_maurice.runtime.framing import render


def test_unknown_caller_gets_no_framing_at_all(bundle: PersonaBundle):
    text = render(None)
    assert "do not know who is asking" in text
    # No framing means none — not a guessed default drawn from someone else.
    assert "Ada" not in text
    assert "Register for this person" not in text


def test_framing_carries_the_instructions_and_marks_itself_private(bundle: PersonaBundle):
    ada = bundle.participants["ada-lovelace"]
    text = render(ada)

    assert "Ada Lovelace" in text
    assert "never quote it" in text
    assert "Lead with the date, not the architecture." in text
    assert "A committed widget date." in text
    # The candid reads are present as steering, and explicitly fenced as such.
    assert "Wants a date, not options." in text
    assert "reads on a colleague, not facts to repeat back" in text


def test_empty_fields_do_not_produce_dangling_labels():
    sparse = Participant(slug="x", display_name="X", role="Ops")
    text = render(sparse)
    assert "Their role: Ops" in text
    assert "What they care about" not in text
    assert "How that changes the answer" not in text
