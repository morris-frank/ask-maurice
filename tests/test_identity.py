"""The alias-table join, from the one direction that is not Slack's.

`from_slack_user` — the join every production caller goes through — is covered in
test_slack.py, next to the edge that feeds it. What is left here is `from_handle`:
`ask --as`, and the resolution rules both directions share.

The claim-shaped entry points (`from_claims`, `email_from_claims`) went with the
Entra edge. Their tests went with them rather than being kept against a function
nothing calls.
"""

from __future__ import annotations

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime.identity import from_handle


def test_from_handle_accepts_name_or_email(bundle: PersonaBundle):
    assert from_handle("Ada Lovelace", bundle).known
    assert from_handle("  ada@testco.com ", bundle).known
    assert not from_handle("", bundle).known


def test_a_resolved_handle_carries_the_participant(bundle: PersonaBundle):
    caller = from_handle("ada@testco.com", bundle)
    assert caller.participant is not None
    assert caller.participant.display_name == "Ada Lovelace"


def test_an_unknown_handle_is_not_an_error(bundle: PersonaBundle):
    """The failure direction that matters: no framing rather than wrong framing.

    An unrecognised asker still gets an answer — they just get Maurice's default
    register instead of one shaped by commentary about somebody else.
    """
    caller = from_handle("stranger@testco.com", bundle)
    assert not caller.known
    assert caller.participant is None
    assert caller.handle == "stranger@testco.com"
