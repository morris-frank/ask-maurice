"""The Slack access edge: request signing, and the slash-command payload.

A different shape of trust from the Entra edge, and worth being explicit about
rather than letting "auth" paper over it.

Entra hands us a per-request assertion, signed by an identity provider, that
names the human who signed in. Slack hands us one shared secret proving *Slack*
sent the request, and a `user_id` inside the body that we take on Slack's word.
Transport authentication and caller identity come apart here: the signature says
the request is genuine, the payload says who asked.

That matters more than it would for an ordinary bot, because per-caller framing
draws on candid per-person commentary. A mis-attributed caller means an answer
shaped by material written about someone else. Two things bound it: Slack sets
`user_id` server-side (a workspace member cannot forge another's), and an
unresolved caller falls back to no framing rather than to a guess.

What genuinely widens is *authorisation*. With a bearer token, access is a named
principal in a tenant. Here it is whoever can invoke the slash command —
workspace and channel membership, administered in Slack rather than in the
stack. The content exposure is bounded (retrieval reads only the shared vault,
which any Soilytix employee can already clone), but the reach is broader, and
that is a product decision rather than an implementation detail.

Verification follows Slack's documented scheme: HMAC-SHA256 over
`v0:{timestamp}:{raw body}`, compared in constant time, with a five-minute
window so a captured request cannot be replayed indefinitely. The signature is
computed over the RAW bytes — re-serialising a parsed form changes the ordering
and escaping, and the digest with it.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import parse_qs

SIGNATURE_HEADER = "x-slack-signature"
TIMESTAMP_HEADER = "x-slack-request-timestamp"

# Slack's signature version prefix. Pinned rather than parsed out of the header:
# accepting whatever version the caller names is how you end up verifying `v0`
# semantics against a `v1` payload.
VERSION = "v0"

# Slack's own recommendation. Bounds replay of a captured request; also tolerates
# ordinary clock skew between their edge and Cloud Run.
MAX_SKEW_SECONDS = 60 * 5

# `response_url` is an outbound POST target lifted from an inbound request. It
# arrives inside a signed body, so it is Slack's — but a delayed answer posted to
# an attacker-chosen URL would be an exfiltration channel, and the check is three
# lines. Defence in depth against the day the verifier above has a bug.
RESPONSE_URL_PREFIX = "https://hooks.slack.com/"


class SlackError(RuntimeError):
    """The request did not come from Slack, or did not parse. Never echoes the body."""


@dataclass(frozen=True)
class SlashCommand:
    """The fields of a slash-command payload this service actually uses."""

    user_id: str
    text: str
    response_url: str
    command: str = ""
    channel_id: str = ""


def verify_signature(
    body: bytes, *, timestamp: str, signature: str, secret: str, now: float | None = None
) -> None:
    """Raise `SlackError` unless `body` was signed by Slack, recently.

    `now` is injectable so the replay window is testable without freezing clocks.
    """
    if not signature or not timestamp:
        raise SlackError("missing signature headers")
    try:
        sent_at = int(timestamp)
    except ValueError:
        raise SlackError("unparseable timestamp") from None

    current = time.time() if now is None else now
    # Symmetric: a timestamp far in the future is as wrong as one far in the past,
    # and only the past case is a replay.
    if abs(current - sent_at) > MAX_SKEW_SECONDS:
        raise SlackError("timestamp outside the replay window")

    base = b":".join((VERSION.encode(), timestamp.encode(), body))
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(f"{VERSION}={digest}", signature):
        raise SlackError("signature mismatch")


def parse_command(body: bytes) -> SlashCommand:
    """Parse a verified slash-command body. Call only after `verify_signature`."""
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        raise SlackError("body is not valid UTF-8") from None

    def first(key: str) -> str:
        values = fields.get(key) or [""]
        return values[0].strip()

    user_id = first("user_id")
    if not user_id:
        raise SlackError("payload carries no user_id")

    response_url = first("response_url")
    if not response_url.startswith(RESPONSE_URL_PREFIX):
        # Includes the empty case: without somewhere to deliver the answer there
        # is no point starting one.
        raise SlackError("response_url is missing or not a Slack webhook URL")

    return SlashCommand(
        user_id=user_id,
        text=first("text"),
        response_url=response_url,
        command=first("command"),
        channel_id=first("channel_id"),
    )
