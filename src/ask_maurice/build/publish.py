"""Push a compiled bundle to GCP Secret Manager as a new version.

This is the only channel between the two planes. The bundle is never committed,
never baked into an image, and never handed to anything but Secret Manager.

Note for whoever writes the Terraform: the runtime service account needs
`roles/secretmanager.secretAccessor` on this one secret. That is a deliberate
divergence from the `kb-mcp` stack, whose SA has no Secret Manager access at all
because its collection ships inside the image. Document it in the stack README
the same way kb-mcp documents its no-IAP exception.
"""

from __future__ import annotations

import json

from ask_maurice.persona import PersonaBundle


def secret_parent(secret_name: str) -> str:
    """Accept either `projects/x/secrets/y` or a full `.../versions/latest` path."""
    return secret_name.split("/versions/", 1)[0]


def publish(bundle: PersonaBundle, secret_name: str) -> str:
    """Add a new version. Returns its resource name."""
    from google.cloud import secretmanager  # imported lazily: build plane only

    client = secretmanager.SecretManagerServiceClient()
    payload = json.dumps(bundle.to_dict(), ensure_ascii=False).encode("utf-8")
    version = client.add_secret_version(
        request={"parent": secret_parent(secret_name), "payload": {"data": payload}}
    )
    return version.name
