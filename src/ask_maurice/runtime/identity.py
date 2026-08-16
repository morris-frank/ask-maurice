"""Who is asking.

Two inbound channels, one join key. Entra hands us verified JWT claims carrying
an email; Slack hands us a user ID that `users.info` turns into an email. Both
end up in `PersonaBundle.aliases`, which was built from person-file frontmatter
at compile time — so this module resolves identity with a dict lookup and never
opens a vault file.

An unresolved caller is not an error. They get a neutral answer with no framing
at all, which is the correct failure direction: no framing is a worse answer, a
wrong framing is a wrong answer about a colleague.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ask_maurice.persona import Participant, PersonaBundle

# Entra puts the address in different claims depending on the app registration
# and account type; check all three in preference order.
_EMAIL_CLAIMS = ("preferred_username", "email", "upn")


@dataclass(frozen=True)
class Caller:
    """A resolved (or unresolved) asker.

    `handle` is safe to log. `participant` is not — it carries bundle content.
    """

    handle: str
    participant: Participant | None = None

    @property
    def known(self) -> bool:
        return self.participant is not None


def email_from_claims(claims: dict[str, Any]) -> str | None:
    for claim in _EMAIL_CLAIMS:
        value = claims.get(claim)
        if isinstance(value, str) and "@" in value:
            return value.strip().lower()
    return None


def from_claims(claims: dict[str, Any], bundle: PersonaBundle) -> Caller:
    email = email_from_claims(claims)
    if email is None:
        subject = str(claims.get("oid") or claims.get("sub") or "unknown")
        return Caller(handle=subject)
    return Caller(handle=email, participant=bundle.resolve(email))


def from_handle(handle: str, bundle: PersonaBundle) -> Caller:
    """Resolve an email, alias or name — what `ask --as` and Slack both end at."""
    return Caller(handle=handle.strip(), participant=bundle.resolve(handle))


def from_slack_user(user_id: str, token: str, bundle: PersonaBundle) -> Caller:
    """Slack user ID -> caller, via `users.info` and then the same alias table.

    The one edge that needs a network round-trip to learn who is asking: Entra
    carries the address inside the assertion, Slack carries only an opaque ID.
    When the lookup fails — token scope, a deactivated account, Slack having a
    bad minute — the caller keeps the raw ID as a handle and gets no framing.
    That is the same failure direction as an unrecognised email: a worse answer,
    not a wrong one about a colleague.
    """
    email = email_from_slack(user_id, token)
    if email is None:
        return Caller(handle=user_id)
    return Caller(handle=email, participant=bundle.resolve(email))


def email_from_slack(user_id: str, token: str) -> str | None:
    """Slack user ID -> email. Needs only `users:read.email`, no history scopes."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        profile = WebClient(token=token).users_info(user=user_id)["user"]["profile"]
    except (SlackApiError, KeyError):
        return None
    email = profile.get("email")
    return email.strip().lower() if isinstance(email, str) else None
