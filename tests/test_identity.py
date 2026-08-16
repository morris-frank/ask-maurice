from __future__ import annotations

from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime.identity import (
    email_from_claims,
    from_claims,
    from_handle,
    from_iap_claims,
)


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


# --- the IAP edge ------------------------------------------------------------
#
# Claim shape per Google's signed-headers spec: plain `email`, and a `sub` of the
# form `accounts.google.com:<id>` which is an identifier, not an address.


def test_iap_claims_resolve_to_a_participant(bundle: PersonaBundle):
    caller = from_iap_claims({"email": "Ada@Testco.com", "sub": "accounts.google.com:1234"}, bundle)
    assert caller.known
    assert caller.participant is not None
    assert caller.participant.display_name == "Ada Lovelace"
    assert caller.handle == "ada@testco.com"


def test_an_iap_caller_with_no_matching_alias_is_answered_unframed(bundle: PersonaBundle):
    caller = from_iap_claims(
        {"email": "stranger@testco.com", "sub": "accounts.google.com:9999"}, bundle
    )
    assert not caller.known
    assert caller.participant is None
    assert caller.handle == "stranger@testco.com"


def test_iap_sub_is_a_handle_not_a_join_key(bundle: PersonaBundle):
    """`sub` is prefixed, never an address — it must not be fed to the alias table."""
    caller = from_iap_claims({"sub": "accounts.google.com:1234"}, bundle)
    assert caller.handle == "accounts.google.com:1234"
    assert not caller.known


def test_entra_only_claims_do_not_resolve_through_the_iap_path(bundle: PersonaBundle):
    """IAP does not mint `preferred_username`; accepting one would be guessing."""
    assert not from_iap_claims({"preferred_username": "ada@testco.com"}, bundle).known


def test_from_handle_accepts_name_or_email(bundle: PersonaBundle):
    assert from_handle("Ada Lovelace", bundle).known
    assert from_handle("  ada@testco.com ", bundle).known
    assert not from_handle("", bundle).known
