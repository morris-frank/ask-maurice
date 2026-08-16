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
VaultRetrieval = Literal["local", "mixedbread", "hybrid"]


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
class MixedbreadConfig:
    """Mixedbread stores: the science literature, and optionally the shared vault.

    Two stores, deliberately separate, because they are different kinds of claim.
    The literature store holds third-party papers — evidence Maurice did not
    produce and must attribute. The vault store, when it exists, holds the same
    shared-vault markdown the local BM25 corpus already reads, just indexed
    semantically; it is Maurice's own writing either way.

    Nothing here can point at private content. The vault store is populated by
    `ask-maurice vault-index`, which reads the shared-vault checkout and nothing
    else, and the persona bundle is never a store input at all.
    """

    api_key: str
    literature_store: str
    vault_store: str
    vault_retrieval: VaultRetrieval

    @property
    def literature_enabled(self) -> bool:
        return bool(self.literature_store)

    @property
    def vault_store_enabled(self) -> bool:
        return self.vault_retrieval != "local"

    @classmethod
    def from_env(cls) -> MixedbreadConfig | None:
        """None when no key is set — the whole integration is then simply off.

        A misconfiguration (a store named without a key, semantic retrieval asked
        for without a store) is an error rather than a silent downgrade: a
        literature path that quietly returns nothing is exactly the failure mode
        `literature.py` was left a stub to avoid.
        """
        retrieval = _optional("ASK_MAURICE_VAULT_RETRIEVAL", "local")
        if retrieval not in ("local", "mixedbread", "hybrid"):
            raise ConfigError(
                "ASK_MAURICE_VAULT_RETRIEVAL must be 'local', 'mixedbread' or 'hybrid', "
                f"got {retrieval!r}"
            )
        literature_store = _optional("ASK_MAURICE_LITERATURE_STORE")
        vault_store = _optional("ASK_MAURICE_VAULT_STORE")
        api_key = _optional("MXBAI_API_KEY")
        if not api_key:
            if literature_store or vault_store or retrieval != "local":
                raise ConfigError(
                    "MXBAI_API_KEY is not set, but a mixedbread store or retrieval mode is "
                    "configured (see .env.example)"
                )
            return None
        if retrieval != "local" and not vault_store:
            raise ConfigError(
                f"ASK_MAURICE_VAULT_RETRIEVAL={retrieval} needs ASK_MAURICE_VAULT_STORE — "
                "index the shared vault with `ask-maurice vault-index` first"
            )
        return cls(
            api_key=api_key,
            literature_store=literature_store,
            vault_store=vault_store,
            vault_retrieval=retrieval,
        )


@dataclass(frozen=True)
class SlackConfig:
    """The Slack access edge. Both values are secrets; neither is ever logged.

    Two credentials because the edge does two jobs. The signing secret verifies
    that a request came from Slack (transport). The bot token turns the `user_id`
    inside that request into an email, which is what joins to the alias table
    (identity). Two credentials for one edge is unusual, and it is the reason
    `runtime/slack.py` argues the trust model out at length rather than assuming
    "signed request" means "known caller".

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
    slack: SlackConfig | None
    mixedbread: MixedbreadConfig | None
    production: bool

    @property
    def has_access_edge(self) -> bool:
        """True when some verified identity can reach us. Framing depends on it.

        One edge today, so this reads as a rename of `slack is not None`. It is
        kept as its own property because the guard below and `/ask` both ask the
        question "can anyone here be identified?", which is not the same question
        as "is Slack configured?" — and the answer stops being the same the moment
        a second edge comes back.
        """
        return self.slack is not None

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        source = _optional("ASK_MAURICE_BUNDLE_SOURCE", "file")
        if source not in ("file", "secret"):
            raise ConfigError(
                f"ASK_MAURICE_BUNDLE_SOURCE must be 'file' or 'secret', got {source!r}"
            )
        production = _optional("ASK_MAURICE_ENV", "development") == "production"
        slack = SlackConfig.from_env()
        # No edge means every caller is anonymous, which in production is both a
        # hole and — because per-caller framing is the product — a uselessly
        # generic service.
        if production and slack is None:
            raise ConfigError(
                "refusing to run unauthenticated in production: configure the Slack "
                "access edge (ASK_MAURICE_SLACK_SIGNING_SECRET + SLACK_BOT_TOKEN)"
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
            slack=slack,
            mixedbread=MixedbreadConfig.from_env(),
            production=production,
        )
