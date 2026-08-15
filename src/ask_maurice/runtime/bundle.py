"""Load the persona bundle. Secret Manager in production, a file in dev.

Loaded once at boot and held in memory. It is never written to disk by the
runtime, never logged, and never included in an error message — a stack trace
that quotes the payload is the leak this whole design exists to prevent.
"""

from __future__ import annotations

import json
from functools import lru_cache

from ask_maurice.config import RuntimeConfig
from ask_maurice.persona import PersonaBundle


class BundleError(RuntimeError):
    """The bundle could not be loaded. Message never contains bundle content."""


def _from_secret(secret_name: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = secret_name if "/versions/" in secret_name else f"{secret_name}/versions/latest"
    return client.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def load(config: RuntimeConfig) -> PersonaBundle:
    if config.bundle_source == "secret":
        raw = _from_secret(config.bundle_secret)
        origin = "Secret Manager"
    else:
        if not config.bundle_path.is_file():
            raise BundleError(
                f"no bundle at {config.bundle_path} — run `ask-maurice build-persona` first"
            )
        raw = config.bundle_path.read_text(encoding="utf-8")
        origin = str(config.bundle_path)

    try:
        return PersonaBundle.from_dict(json.loads(raw))
    except (ValueError, TypeError, KeyError) as exc:
        # Deliberately does not include `raw` or the parser's excerpt of it.
        raise BundleError(
            f"bundle from {origin} is not a valid v1 bundle: {type(exc).__name__}"
        ) from None


@lru_cache(maxsize=1)
def cached(config: RuntimeConfig) -> PersonaBundle:
    return load(config)
