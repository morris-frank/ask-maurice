"""HTTP surface. Three access edges; any of them is how we know who asked.

**Entra bearer token.** Same verification shape as `kb-ingest`: RS256, JWKS from
the tenant's discovery endpoint, issuer and audience both pinned, `tid` checked
against the configured tenant so a token from another tenant with a matching
audience is rejected. This is the edge an MCP client or a script uses.

**Google IAP assertion.** Deployed on Cloud Run behind IAP, the edge is Google's:
the caller authenticates there and we receive a signed assertion header. Nothing
about that verification is shared with the Entra path — different algorithm
(ES256, not RS256), different keys (Google's static JWK set, not a tenant's
JWKS), different issuer, different email claim. Hence a separate verifier rather
than a parameterised one; the two only meet at `Caller`.

**Slack signed request.** The team-facing surface, and the odd one out: it does
not use `resolve_caller` at all. The signature authenticates *Slack*, not a
person, and the human's identity arrives in the payload — so it gets its own
route with its own verifier and its own identity join. See `runtime/slack.py`
for what that trades away. It is also the only route that answers
asynchronously, because Slack's three-second deadline and a considered answer
are not compatible.

Auth is required whenever `ASK_MAURICE_ENV=production` — `RuntimeConfig` refuses
to construct without at least one edge, so an unauthenticated production deploy
fails at boot rather than at the first request. That guard is doing more work
here than in a typical service: an unidentified caller still gets an answer, but
an unframed one, and the framing is the product.

Logging: no request body, no answer text, no framing. Only the caller handle and
whether they resolved. The root logger's handlers carry the redaction filter, so
even an accidental log of prompt content is scrubbed on the way out.
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from slack_sdk.webhook import WebhookClient

from ask_maurice.config import EntraConfig, IapConfig, RuntimeConfig
from ask_maurice.persona import PersonaBundle
from ask_maurice.runtime import bundle as bundle_mod
from ask_maurice.runtime import redaction
from ask_maurice.runtime import slack as slack_mod
from ask_maurice.runtime.agent import Agent, AgentError, Answer
from ask_maurice.runtime.corpus import Corpus, CorpusError
from ask_maurice.runtime.identity import (
    Caller,
    from_claims,
    from_handle,
    from_iap_claims,
    from_slack_user,
)

log = logging.getLogger(__name__)

# Set by IAP itself and stripped from any inbound request, so its presence is
# meaningful only because IAP is the only thing that can reach the service.
# We verify the signature regardless — see the module docstring.
IAP_HEADER = "x-goog-iap-jwt-assertion"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    # Dev-only override, ignored whenever a verified token is present.
    as_handle: str | None = Field(default=None, alias="as")


class AskResponse(BaseModel):
    answer: str
    sources: list[str]
    artifact: str
    artifact_available: bool


def verify(token: str, config: EntraConfig) -> dict[str, Any]:
    """Verified claims, or raise. Never logs or echoes the token."""
    signing_key = jwt.PyJWKClient(config.jwks_uri).get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=config.audience,
        issuer=config.issuer,
        options={"require": ["exp", "aud", "iss"]},
    )
    if claims.get("tid") != config.tenant_id:
        raise jwt.InvalidTokenError("token is from a different tenant")
    return claims


def verify_iap(token: str, config: IapConfig) -> dict[str, Any]:
    """Verified IAP assertion claims, or raise. Never logs or echoes the token.

    `algorithms=["ES256"]` is a whitelist, not a hint — IAP signs with ES256 and
    accepting anything else here would let a token signed with a different
    algorithm against the same key material through.
    """
    signing_key = jwt.PyJWKClient(config.jwks_uri).get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256"],
        audience=config.audience,
        issuer=config.issuer,
        options={"require": ["exp", "aud", "iss"]},
    )


def resolve_caller(
    authorization: str, iap_assertion: str, config: RuntimeConfig, persona: PersonaBundle
) -> Caller:
    """Verified bearer token -> verified IAP assertion -> anonymous.

    Bearer first because it is the more specific signal: behind IAP *every*
    request carries an assertion, so an assertion plus a bearer token means a
    client that deliberately authenticated as itself, and that is the identity
    to honour.

    Anonymous is the last resort and stays development-only — with an edge
    configured, a request that satisfies neither is rejected rather than quietly
    downgraded to an unframed answer.
    """
    if config.entra is not None and authorization.lower().startswith("bearer "):
        try:
            claims = verify(authorization.split(" ", 1)[1], config.entra)
        except jwt.PyJWTError as exc:
            # Reason to the log, never to the caller and never with the token.
            log.info("bearer token rejected: %s", type(exc).__name__)
            raise HTTPException(401, "invalid token") from None
        return from_claims(claims, persona)

    if config.iap is not None and iap_assertion:
        try:
            claims = verify_iap(iap_assertion, config.iap)
        except jwt.PyJWTError as exc:
            log.info("IAP assertion rejected: %s", type(exc).__name__)
            raise HTTPException(401, "invalid IAP assertion") from None
        return from_iap_claims(claims, persona)

    if not config.has_access_edge:
        return Caller(handle="anonymous")
    raise HTTPException(401, "bearer token or IAP assertion required")


def slack_answer_text(answer: Answer) -> str:
    """Format an answer for Slack. Sources inline; the artifact hint if there is one."""
    parts = [answer.text]
    if answer.sources:
        parts.append("_sources: " + ", ".join(answer.sources) + "_")
    if answer.suggestion.kind.value != "none":
        availability = "" if answer.suggestion.available else " (not wired up yet)"
        parts.append(f"_artifact candidate: {answer.suggestion.kind.value}{availability}_")
    return "\n\n".join(parts)


def create_app(config: RuntimeConfig | None = None) -> FastAPI:
    config = config or RuntimeConfig.from_env()
    persona = bundle_mod.load(config)
    redaction.install(redaction.Redactor(persona))
    corpus = Corpus(root=config.corpus_path, include_transcripts=config.include_transcripts)
    agent = Agent.build(persona, corpus)

    app = FastAPI(title="ask-maurice", docs_url=None, redoc_url=None)

    def caller(request: Request) -> Caller:
        return resolve_caller(
            request.headers.get("authorization", ""),
            request.headers.get(IAP_HEADER, ""),
            config,
            persona,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "persona": persona.source_commit}

    @app.get("/.well-known/oauth-protected-resource")
    def protected_resource() -> dict[str, Any]:
        """RFC 9728 discovery, so MCP clients can find the right authorisation server."""
        if config.entra is None:
            raise HTTPException(404, "not configured")
        return {
            "resource": config.entra.resource_url,
            "authorization_servers": [config.entra.issuer],
            "bearer_methods_supported": ["header"],
        }

    def deliver_slack_answer(command: slack_mod.SlashCommand) -> None:
        """Answer, then post to the pre-signed `response_url`. Runs after the ack.

        Nothing here may raise into the caller's face — the HTTP response went out
        long ago. Every failure becomes a message in the thread instead, because a
        slash command that silently produces nothing is indistinguishable from a
        broken integration.
        """
        assert config.slack is not None  # noqa: S101 - the route checked; this is for ty
        who = from_slack_user(command.user_id, config.slack.bot_token, persona)
        log.info("slack question from %s (resolved=%s)", who.handle, who.known)
        try:
            text = slack_answer_text(agent.answer(command.text, who))
        except (AgentError, CorpusError) as exc:
            # Both are documented as carrying no prompt or bundle content.
            text = f":warning: {exc}"
        except Exception:
            log.exception("slack answer failed for %s", who.handle)
            text = ":warning: Something broke on my side. The error is in the logs."
        try:
            WebhookClient(command.response_url).send(text=text, response_type="ephemeral")
        except Exception:
            # The answer is already lost; at least do not take the worker with it.
            log.exception("could not deliver slack answer to %s", who.handle)

    @app.post("/slack/command")
    async def slack_command(request: Request, background: BackgroundTasks) -> dict[str, str]:
        """Slash command entry point. Verifies, acks inside 3s, answers later.

        Slack wants a 200 within three seconds; an opus-5 answer at `xhigh` effort
        over the vault is nowhere near that. So this returns an immediate holding
        reply and hands the real work to a background task, which posts to the
        payload's `response_url` when it finishes. That deferral is why the Cloud
        Run service needs CPU allocated outside a request — with the default
        throttling the background task stalls the moment the ack returns.

        The delayed answer is ephemeral: it was shaped for one person using
        commentary about them, so it goes back to that person rather than the
        channel. Flip `response_type` if the team decides shared answers are worth
        more than that.
        """
        if config.slack is None:
            raise HTTPException(404, "slack edge is not configured")
        raw = await request.body()
        try:
            slack_mod.verify_signature(
                raw,
                timestamp=request.headers.get(slack_mod.TIMESTAMP_HEADER, ""),
                signature=request.headers.get(slack_mod.SIGNATURE_HEADER, ""),
                secret=config.slack.signing_secret,
            )
            command = slack_mod.parse_command(raw)
        except slack_mod.SlackError as exc:
            # Reason to the log; the caller learns only that it was rejected.
            log.info("slack request rejected: %s", exc)
            raise HTTPException(401, "invalid slack request") from None

        if not command.text:
            return {"response_type": "ephemeral", "text": "Ask me something after the command."}
        background.add_task(deliver_slack_answer, command)
        return {"response_type": "ephemeral", "text": "Thinking — I'll follow up here shortly."}

    @app.post("/ask", response_model=AskResponse)
    def ask(body: AskRequest, who: Caller = Depends(caller)) -> AskResponse:  # noqa: B008
        if who.handle == "anonymous" and body.as_handle and not config.production:
            who = from_handle(body.as_handle, persona)
        log.info("question from %s (resolved=%s)", who.handle, who.known)
        try:
            answer = agent.answer(body.question, who)
        except AgentError as exc:
            raise HTTPException(502, str(exc)) from None
        return AskResponse(
            answer=answer.text,
            sources=answer.sources,
            artifact=answer.suggestion.kind.value,
            artifact_available=answer.suggestion.available,
        )

    return app
