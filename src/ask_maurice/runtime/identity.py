"""Who is asking.

Three inbound channels, one join key. Entra hands us verified JWT claims carrying
an email; Google IAP hands us a different verified JWT carrying its own; Slack
hands us a user ID that `users.info` turns into an email. All three end up in
`PersonaBundle.aliases`, which was built from person-file frontmatter at compile
time — so this module resolves identity with a dict lookup and never opens a
vault file.

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

# IAP is simpler and stricter: one documented claim, `email`, carrying the plain
# address. Its `sub` is *not* an address — it is `accounts.google.com:<id>` — so
# the fallback below uses it as a handle only, never as a join key.
_IAP_EMAIL_CLAIM = "email"


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


def from_iap_claims(claims: dict[str, Any], bundle: PersonaBundle) -> Caller:
    """Verified IAP assertion -> caller, through the same alias table as Entra.

    Kept separate from `from_claims` rather than folded into it: the two token
    shapes agree on nothing but the word "email", and checking Entra's three
    claims against an IAP token would silently accept `preferred_username` from
    an issuer that does not mint one.
    """
    email = claims.get(_IAP_EMAIL_CLAIM)
    if not isinstance(email, str) or "@" not in email:
        return Caller(handle=str(claims.get("sub") or "unknown"))
    email = email.strip().lower()
    return Caller(handle=email, participant=bundle.resolve(email))


def from_handle(handle: str, bundle: PersonaBundle) -> Caller:
    """Resolve an email, alias or name — what `ask --as` and Slack both end at."""
    return Caller(handle=handle.strip(), participant=bundle.resolve(handle))


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
