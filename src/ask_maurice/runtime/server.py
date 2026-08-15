"""HTTP surface. Entra-verified callers; the token is how we know who asked.

Same verification shape as `kb-ingest`: RS256, JWKS from the tenant's discovery
endpoint, issuer and audience both pinned, `tid` checked against the configured
tenant so a token from another tenant with a matching audience is rejected.

Auth is required whenever `ASK_MAURICE_ENV=production` — `RuntimeConfig` refuses
to construct otherwise, so an unauthenticated production deploy fails at boot
rather than at the first request.

Logging: no request body, no answer text, no framing. Only the caller handle and
whether they resolved. The root logger's handlers carry the redaction filter, so
even an accidental log of prompt content is scrubbed on the way out.
"""

from __future__ import annotations

import logging
from typing import Any

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ask_maurice.config import EntraConfig, RuntimeConfig
from ask_maurice.runtime import bundle as bundle_mod
from ask_maurice.runtime import redaction
from ask_maurice.runtime.agent import Agent, AgentError
from ask_maurice.runtime.corpus import Corpus
from ask_maurice.runtime.identity import Caller, from_claims, from_handle

log = logging.getLogger(__name__)


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


def create_app(config: RuntimeConfig | None = None) -> FastAPI:
    config = config or RuntimeConfig.from_env()
    persona = bundle_mod.load(config)
    redaction.install(redaction.Redactor(persona))
    corpus = Corpus(root=config.corpus_path, include_transcripts=config.include_transcripts)
    agent = Agent.build(persona, corpus)

    app = FastAPI(title="ask-maurice", docs_url=None, redoc_url=None)

    def caller(request: Request) -> Caller:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            if config.entra is None:
                return Caller(handle="anonymous")
            raise HTTPException(401, "bearer token required")
        if config.entra is None:
            return Caller(handle="anonymous")
        try:
            claims = verify(header.split(" ", 1)[1], config.entra)
        except jwt.PyJWTError as exc:
            # Reason to the log, never to the caller and never with the token.
            log.info("token rejected: %s", type(exc).__name__)
            raise HTTPException(401, "invalid token") from None
        return from_claims(claims, persona)

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
