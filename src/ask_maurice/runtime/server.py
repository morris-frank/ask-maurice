"""HTTP surface. One access edge, and a dev-only way in behind it.

**Slack signed request** is the whole of production. The signature authenticates
*Slack*, not a person, and the human's identity arrives in the payload — so
`/slack/command` carries its own verifier and its own identity join rather than
going through `resolve_caller`. See `runtime/slack.py` for what that trades away.
It is also the only route that answers asynchronously, because Slack's
three-second deadline and a considered answer are not compatible.

**`/ask` has no edge of its own.** It answers anonymously when nothing is
configured — a local `serve` — and 401s otherwise, which in production is
everyone. It is kept because it is the seam an authenticated HTTP surface would
reattach to, and because the local loop is easier to poke at over HTTP than
through the CLI. It is not a way in: nothing can authenticate to it today.

Auth is required whenever `ASK_MAURICE_ENV=production` — `RuntimeConfig` refuses
to construct without an access edge, so an unauthenticated production deploy
fails at boot rather than at the first request. That guard is doing more work
here than in a typical service: an unidentified caller still gets an answer, but
an unframed one, and the framing is the product.

Logging: no request body, no answer text, no framing. Only the caller handle and
whether they resolved. The root logger's handlers carry the redaction filter, so
even an accidental log of prompt content is scrubbed on the way out.
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from slack_sdk.webhook import WebhookClient

from ask_maurice.config import RuntimeConfig
from ask_maurice.runtime import bundle as bundle_mod
from ask_maurice.runtime import literature as literature_mod
from ask_maurice.runtime import redaction, retrieval
from ask_maurice.runtime import slack as slack_mod
from ask_maurice.runtime.agent import Agent, AgentError, Answer
from ask_maurice.runtime.corpus import Corpus, CorpusError
from ask_maurice.runtime.identity import Caller, from_handle, from_slack_user

log = logging.getLogger(__name__)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    # Dev-only override, ignored whenever a verified token is present.
    as_handle: str | None = Field(default=None, alias="as")


class AskResponse(BaseModel):
    answer: str
    sources: list[str]
    # Papers, kept in their own field for the same reason the agent keeps them
    # in their own list: a vault path and a citation are different warrants.
    references: list[str] = []
    artifact: str
    artifact_available: bool


def resolve_caller(config: RuntimeConfig) -> Caller:
    """Anonymous with no edge configured; 401 otherwise.

    There is no token to check any more, so this is a policy decision rather than
    a verification: `/ask` is open exactly when nothing on this deployment can
    identify anybody, which is the local case. As soon as an edge exists —
    today that means Slack — `/ask` stops answering, because a route that hands
    out unframed answers to whoever found the URL is not a smaller version of the
    product, it is a different one.

    Kept as a named function rather than inlined into the dependency: it is the
    seam an authenticated HTTP surface reattaches to, and the place the decision
    is written down.
    """
    if not config.has_access_edge:
        return Caller(handle="anonymous")
    raise HTTPException(401, "no authenticated HTTP edge is configured; use the Slack command")


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
    literature = literature_mod.from_config(config.mixedbread)
    log.info("%s", literature_mod.status(literature))
    agent = Agent.build(persona, retrieval.for_config(config.mixedbread, corpus), literature)

    app = FastAPI(title="ask-maurice", docs_url=None, redoc_url=None)

    def caller() -> Caller:
        return resolve_caller(config)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "persona": persona.source_commit}

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
            references=answer.references,
            artifact=answer.suggestion.kind.value,
            artifact_available=answer.suggestion.available,
        )

    return app
