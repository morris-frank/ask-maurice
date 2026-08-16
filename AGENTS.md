# ask-maurice — working agreement

A hosted agent that answers Soilytix team questions in Maurice's voice, from the
shared vault, framed for whoever asked. See `README.md` for the shape; this file
is the rules.

## Sensitivity boundary — read first

This repo straddles a boundary that already exists in the vault: `origin`
(`morris-frank/vault`, private, everything) vs `team` (`Soilytix/vault`, the
graduated subset). `.kbignore` in the vault names what never crosses, and
`.bin/kb` is the only tool that moves content across it.

Three rules follow, and none of them are negotiable:

1. **The runtime never touches the private vault.** Retrieval reads `corpus/`,
   a clone of the team remote, and nothing else. No code path in `runtime/`
   may open a path under `ASK_MAURICE_PRIVATE_VAULT`. Tests enforce this.
2. **The compiled persona bundle is a sensitive asset.** It is built from
   `.kbignore`'d paths (`people/`, `me/`) and contains candid per-person
   commentary about people who can call this agent. It never enters git, never
   enters a container image, and never appears in a log, a trace, an error
   message, or a model response. Build plane → Secret Manager → memory. That is
   the whole lifecycle.
3. **If content looks like it is crossing the boundary, stop and ask.** Do not
   copy, summarise or redact private content into a tracked path to make it
   fit. That is the vault's own `AGENTS.md` rule and it applies here verbatim.

## Golden rules

1. **mise owns the toolchain.** Python, uv, ruff, ty, gitleaks, osv-scanner and
   prek are pinned in `mise.toml` and locked in `mise.lock`.
2. **Invoke pinned tools via `mise exec --` or a `mise run` task**, never a bare
   tool name — guards against a stray same-named binary earlier on PATH.
3. **`persona/` and `corpus/` are gitignored** and never leave the machine that
   produced them. `persona/` additionally has a pre-commit hook refusing it.
4. **New Python deps land via `uv add` / `uv add --group dev`**, never a
   hand-edited `pyproject.toml`/`uv.lock`.
5. **Model calls go through the official `anthropic` SDK**, never raw HTTP.
   Model string is exactly `claude-opus-5`; thinking is adaptive (`{"type":
   "adaptive"}`); `budget_tokens`, `temperature`, `top_p`, `top_k` and assistant
   prefill are all 400s on this model — do not add them back.
6. **The persona block is the cached prefix; per-caller framing goes after it.**
   Prompt caching is a prefix match rendered `tools` → `system` → `messages`, so
   the frozen bundle lives in `system` with `cache_control`, and the framing for
   this caller is a mid-conversation system message inside `messages[]`. Putting
   framing in `system` would invalidate the shared cache on every caller.
7. **`mise run check` is the definition of done.**

## Layout

```
src/ask_maurice/
  cli.py              Typer app: build-persona, publish-persona, corpus-sync, bake-corpus,
                      ask, serve
  config.py           BuildConfig (private vault) and RuntimeConfig (shared only) — the
                      split is load-bearing; RuntimeConfig has no private-vault field.
                      EntraConfig, IapConfig and SlackConfig are the three access
                      edges; production requires at least one
  bake.py             neither plane: ./corpus -> dist/corpus for the image. Calls the
                      runtime's Corpus.documents() so the baked set cannot drift from
                      the retrievable set; writes HEAD to a COMMIT file beside them
  persona.py          PersonaBundle / Participant: the data model both planes share.
                      Pure data, so runtime never imports anything from build/
  build/
    vault.py            markdown + frontmatter from the PRIVATE vault (build plane only)
    compile.py          person file + advisor dict + style notes -> PersonaBundle, and
                        the dict-key -> person -> aliases join that makes the runtime's
                        identity lookup a plain dict get
    publish.py          push the bundle to GCP Secret Manager as a new version
  runtime/
    bundle.py           load the bundle (Secret Manager in prod, local file in dev)
    identity.py         Entra claims / IAP claims / Slack user ID -> email -> Participant,
                        via the alias table baked into the bundle. Opens no vault file.
    slack.py            the Slack edge: HMAC-SHA256 request signing with a replay
                        window, and slash-command parsing. Read its docstring before
                        changing it — the signature authenticates Slack, not a person,
                        and that trade is deliberate
    framing.py          participant record -> the per-caller framing block
    corpus.py           shared-vault clone, sync, and lexical retrieval with provenance.
                        Provenance is .git in dev or a COMMIT file in a baked image;
                        neither one present is a hard error, never a silent answer
    artifacts.py        classify a question -> document | podcast | explainer-video | none
    literature.py       STUB: kb-mcp science lookup, deferred out of v1
    prompt.py           cached persona in system[], per-caller framing in messages[]
    agent.py            the anthropic call on claude-opus-5
    redaction.py        scrub bundle-derived text from logs and answers; refusal text
    server.py           FastAPI app, RFC 9728 metadata, and all three access edges:
                        verify() for Entra bearer tokens (RS256, tenant JWKS) and
                        verify_iap() for IAP assertions (ES256, Google's JWK set).
                        resolve_caller() is the order: bearer -> IAP -> anonymous.
                        /slack/command bypasses that entirely — its own verifier, its
                        own identity join, and the only route that answers async
                        (Slack's 3s ack; needs CPU-always-allocated on Cloud Run)
scripts/
  no_persona_bundle.py  pre-commit guard: refuses a compiled bundle by path or content
tests/                pure logic only, plus test_boundary.py — which fails if any module
                      under runtime/ imports the build plane or names the private vault
Dockerfile            Cloud Run image: uv sync --frozen --no-dev, non-root, port 8080,
                      COPY dist/corpus (never corpus/), and no persona bundle at all
```

## Divergences from `doppel-maurice` worth knowing

- That project is single-user local tooling with no server. This one is hosted,
  multi-caller, and identity-aware — so it carries auth, redaction and a
  sensitivity boundary that project does not need.
- That project's secrets module has a Secret Manager fallback for *inputs*. Here
  Secret Manager holds the *compiled artefact itself*, which is the reason the
  runtime service account needs read access at all.

## Definition of done

- `mise run check` is green.
- New logic has a direct test. Anything touching the sensitivity boundary has a
  test that fails if the boundary moves.
- New deps via `uv add`, never hand-edited.
