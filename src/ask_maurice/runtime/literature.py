"""Science-literature lookup via kb-mcp. NOT IMPLEMENTED — deliberately.

A science question often needs a source the vault does not hold. `kb-mcp`
already serves that corpus over MCP with Entra-verified callers, so wiring it in
is a matter of adding an MCP client here and a tool definition in `agent.py`.

It is out of v1 on purpose: a half-wired literature path that silently returns
nothing is worse than none, because the agent cannot tell "no result" from "not
connected" and will answer as if the literature agreed with it. Until this is
real, the standing rules in `prompt.py` tell the model to name the gap instead.

When implementing: the agent must still cite, the answer must still distinguish
a vault claim from a literature claim, and a kb-mcp outage must degrade to "I
can't check the literature right now" rather than to silence.
"""

from __future__ import annotations

AVAILABLE = False


def status() -> str:
    return "kb-mcp literature lookup is not wired up; answers are vault-only."
