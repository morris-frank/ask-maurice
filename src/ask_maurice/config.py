"""Environment-derived configuration for both planes.

Split deliberately in two. `BuildConfig` is the only place the private vault
path is read; nothing under `runtime/` imports it. `RuntimeConfig` knows about
the shared-vault clone, the bundle source and caller identity, and has no field
that could point at private content.

Everything fails loudly on a missing required value rather than guessing a
default — a silently-wrong vault path here means either an empty agent or a
boundary violation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BundleSource = Literal["file", "secret"]


class ConfigError(RuntimeError):
    """A required environment variable is missing or unusable."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set (see .env.example)")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BuildConfig:
    """Build plane. Runs on Maurice's machine, reads the private vault."""

    private_vault: Path
    bundle_path: Path
    secret_name: str

    @classmethod
    def from_env(cls) -> BuildConfig:
        vault = Path(_require("ASK_MAURICE_PRIVATE_VAULT")).expanduser()
        if not (vault / "people").is_dir():
            raise ConfigError(f"{vault} does not look like the private vault (no people/ dir)")
        return cls(
            private_vault=vault,
            bundle_path=Path(_optional("ASK_MAURICE_BUNDLE_PATH", "./persona/bundle.json")),
            secret_name=_optional("ASK_MAURICE_BUNDLE_SECRET"),
        )


@dataclass(frozen=True)
class EntraConfig:
    """Per-caller identity, same shape as kb-ingest's verifier config."""

    tenant_id: str
    audience: str
    resource_url: str

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def jwks_uri(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"

    @classmethod
    def from_env(cls) -> EntraConfig | None:
        tenant_id = _optional("ASK_MAURICE_ENTRA_TENANT_ID")
        audience = _optional("ASK_MAURICE_ENTRA_AUDIENCE")
        resource_url = _optional("ASK_MAURICE_RESOURCE_URL")
        if not (tenant_id and audience and resource_url):
            return None
        return cls(tenant_id=tenant_id, audience=audience, resource_url=resource_url)


@dataclass(frozen=True)
class IapConfig:
    """The other access edge: Google IAP in front of Cloud Run.

    Behind IAP the caller never presents a bearer token to us — Google's edge
    authenticates them and forwards a signed assertion. Verified against Google's
    own keys, so unlike `EntraConfig` there is nothing tenant-shaped to configure;
    the audience alone pins the token to this exact service.

    That audience string is not free-form. For a Cloud Run service IAP mints it
    as `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME` — note
    the leading slash, the *number* rather than the project ID, and that it
    differs from the load-balancer form (`/projects/N/global/backendServices/ID`)
    and the App Engine form (`/projects/N/apps/PROJECT_ID`). A mismatch here is
    a 401 on every request, so it is taken verbatim from the environment and
    never assembled from parts.
    """

    audience: str

    # Fixed by Google, not by our deployment — hence constants rather than config.
    ISSUER = "https://cloud.google.com/iap"
    JWKS_URI = "https://www.gstatic.com/iap/verify/public_key-jwk"

    @property
    def issuer(self) -> str:
        return self.ISSUER

    @property
    def jwks_uri(self) -> str:
        return self.JWKS_URI

    @classmethod
    def from_env(cls) -> IapConfig | None:
        audience = _optional("ASK_MAURICE_IAP_AUDIENCE")
        return cls(audience=audience) if audience else None


@dataclass(frozen=True)
class SlackConfig:
    """The Slack access edge. Both values are secrets; neither is ever logged.

    Two credentials because the edge does two jobs. The signing secret verifies
    that a request came from Slack (transport). The bot token turns the `user_id`
    inside that request into an email, which is what joins to the alias table
    (identity). Slack is the one edge where those are separate credentials —
    Entra and IAP carry the address inside the assertion itself.

    `users:read.email` is the only scope needed to resolve a caller; posting a
    delayed answer goes to the payload's `response_url`, which is pre-signed and
    needs no scope at all.
    """

    signing_secret: str
    bot_token: str

    @classmethod
    def from_env(cls) -> SlackConfig | None:
        signing_secret = _optional("ASK_MAURICE_SLACK_SIGNING_SECRET")
        bot_token = _optional("SLACK_BOT_TOKEN")
        # Both or nothing. A signing secret without a token verifies requests and
        # then frames nobody, which is the failure that looks like a working
        # service — exactly what the access-edge guard exists to prevent.
        if not (signing_secret and bot_token):
            return None
        return cls(signing_secret=signing_secret, bot_token=bot_token)


@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime plane. Hosted. Has no path into the private vault, by design."""

    corpus_path: Path
    corpus_remote: str
    corpus_ref: str
    include_transcripts: bool
    bundle_source: BundleSource
    bundle_path: Path
    bundle_secret: str
    entra: EntraConfig | None
    iap: IapConfig | None
    slack: SlackConfig | None
    production: bool

    @property
    def has_access_edge(self) -> bool:
        """True when some verified identity can reach us. Framing depends on it."""
        return self.entra is not None or self.iap is not None or self.slack is not None

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        source = _optional("ASK_MAURICE_BUNDLE_SOURCE", "file")
        if source not in ("file", "secret"):
            raise ConfigError(
                f"ASK_MAURICE_BUNDLE_SOURCE must be 'file' or 'secret', got {source!r}"
            )
        production = _optional("ASK_MAURICE_ENV", "development") == "production"
        entra = EntraConfig.from_env()
        iap = IapConfig.from_env()
        slack = SlackConfig.from_env()
        # Any one edge will do, and combinations are normal: Slack for the team,
        # IAP for a browser, a bearer token for an MCP client. None of them means
        # every caller is anonymous, which in production is both a hole and —
        # because per-caller framing is the product — a uselessly generic service.
        if production and entra is None and iap is None and slack is None:
            raise ConfigError(
                "refusing to run unauthenticated in production: configure at least one "
                "access edge — Slack (ASK_MAURICE_SLACK_SIGNING_SECRET + SLACK_BOT_TOKEN), "
                "IAP (ASK_MAURICE_IAP_AUDIENCE), or Entra bearer tokens "
                "(ASK_MAURICE_ENTRA_TENANT_ID + ASK_MAURICE_ENTRA_AUDIENCE + "
                "ASK_MAURICE_RESOURCE_URL)"
            )
        secret = _require("ASK_MAURICE_BUNDLE_SECRET") if source == "secret" else ""
        return cls(
            corpus_path=Path(_optional("ASK_MAURICE_CORPUS", "./corpus")).expanduser(),
            corpus_remote=_optional(
                "ASK_MAURICE_CORPUS_REMOTE", "git@github.com:Soilytix/vault.git"
            ),
            corpus_ref=_optional("ASK_MAURICE_CORPUS_REF", "main"),
            include_transcripts=_flag("ASK_MAURICE_INCLUDE_TRANSCRIPTS"),
            bundle_source=source,
            bundle_path=Path(_optional("ASK_MAURICE_BUNDLE_PATH", "./persona/bundle.json")),
            bundle_secret=secret,
            entra=entra,
            iap=iap,
            slack=slack,
            production=production,
        )
