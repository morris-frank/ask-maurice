from __future__ import annotations

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime.identity import email_from_claims, from_claims, from_handle


def test_email_claim_preference_order():
    assert email_from_claims({"preferred_username": "A@Testco.com", "email": "b@testco.com"}) == (
        "a@testco.com"
    )
    assert email_from_claims({"upn": "c@testco.com"}) == "c@testco.com"
    # A `preferred_username` that isn't an address (some app registrations) falls through.
    assert email_from_claims({"preferred_username": "ada", "email": "d@testco.com"}) == (
        "d@testco.com"
    )
    assert email_from_claims({"oid": "1234"}) is None


def test_verified_claims_resolve_to_a_participant(bundle: PersonaBundle):
    caller = from_claims({"preferred_username": "ada@testco.com", "oid": "x"}, bundle)
    assert caller.participant is not None
    assert caller.known
    assert caller.participant.display_name == "Ada Lovelace"


def test_unknown_caller_is_not_an_error(bundle: PersonaBundle):
    caller = from_claims({"preferred_username": "stranger@testco.com"}, bundle)
    assert not caller.known
    assert caller.participant is None
    assert caller.handle == "stranger@testco.com"


def test_caller_without_an_email_claim_falls_back_to_the_object_id(bundle: PersonaBundle):
    caller = from_claims({"oid": "00000000-aaaa", "sub": "ignored"}, bundle)
    assert caller.handle == "00000000-aaaa"
    assert not caller.known


def test_from_handle_accepts_name_or_email(bundle: PersonaBundle):
    assert from_handle("Ada Lovelace", bundle).known
    assert from_handle("  ada@testco.com ", bundle).known
    assert not from_handle("", bundle).known
