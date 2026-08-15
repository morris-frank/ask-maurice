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
    production: bool

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        source = _optional("ASK_MAURICE_BUNDLE_SOURCE", "file")
        if source not in ("file", "secret"):
            raise ConfigError(
                f"ASK_MAURICE_BUNDLE_SOURCE must be 'file' or 'secret', got {source!r}"
            )
        production = _optional("ASK_MAURICE_ENV", "development") == "production"
        entra = EntraConfig.from_env()
        if production and entra is None:
            raise ConfigError(
                "refusing to run unauthenticated in production: set "
                "ASK_MAURICE_ENTRA_TENANT_ID / _AUDIENCE / ASK_MAURICE_RESOURCE_URL"
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
            production=production,
        )
