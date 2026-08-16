"""Who is asking.

One inbound channel, one join key. Slack hands us a user ID that `users.info`
turns into an email, which lands in `PersonaBundle.aliases` — built from
person-file frontmatter at compile time, so this module resolves identity with a
dict lookup and never opens a vault file. `from_handle` is the same lookup from
the other direction, for `ask --as`.

The email is the join key rather than any provider's own identifier, which is
what kept this module unchanged when the Entra and IAP edges were removed: an
edge decides how an address is proven, not what it means.

An unresolved caller is not an error. They get a neutral answer with no framing
at all, which is the correct failure direction: no framing is a worse answer, a
wrong framing is a wrong answer about a colleague.
"""

from __future__ import annotations

from dataclasses import dataclass

from ask_maurice.persona import Participant, PersonaBundle


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


def from_handle(handle: str, bundle: PersonaBundle) -> Caller:
    """Resolve an email, alias or name — what `ask --as` and Slack both end at."""
    return Caller(handle=handle.strip(), participant=bundle.resolve(handle))


def from_slack_user(user_id: str, token: str, bundle: PersonaBundle) -> Caller:
    """Slack user ID -> caller, via `users.info` and then the same alias table.

    An edge that needs a network round-trip to learn who is asking, because Slack
    carries only an opaque ID where a signed assertion would carry the address.
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
