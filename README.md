# ask-maurice

A hosted doppelgänger agent. Someone on the Soilytix team asks a question — in
Slack, or over MCP/HTTP — and gets an answer in Maurice's voice, grounded in the
**shared** Soilytix vault, framed for whoever asked.

Three things make it more than a wrapper around a chat model:

1. **Persona is fixed, not prompted per call.** A build step compiles Maurice's
   own person file, style notes and the communication-advisor dict into one
   frozen bundle. Callers cannot change it.
2. **The asker is known, so the answer is framed.** Slack tells us the user ID,
   Entra tells us the email. Both resolve to a person file in the vault, which
   carries that person's role, what they care about, and how Maurice's default
   register should shift for them.
3. **Artifacts are proposed, not only produced on request.** An explainer
   question that feeds off the technical docs is a candidate for a written
   document, a NotebookLM podcast, or an explainer video. The agent decides
   whether the question warrants one and says so.

## The two planes

This is the load-bearing design decision. Read it before changing anything.

```
BUILD PLANE (Maurice's machine only, never hosted)
  private vault (origin: morris-frank/vault)
    people/*.md                 ← .kbignore'd, never graduates to the team remote
    people/…team prompt dict.json
    me/*.md
        │
        │  ask-maurice build-persona
        ▼
    persona bundle (JSON)  ── ask-maurice publish-persona ──▶  GCP Secret Manager
                                                                       │
────────────────────────────────────────────────────────────────────── │ ────────
RUNTIME PLANE (Cloud Run; Entra bearer and/or IAP at the edge)         │
                                                                       ▼
    baked corpus (team: Soilytix/vault @ main)                persona bundle
    564 markdown files + COMMIT, baked into the image         loaded at boot,
        │                                                     held in memory only
        └──── retrieval ────▶  agent  ◀──── framing for the caller ────┘
                                 │
                                 ▼
                          answer (+ optional artifact)
```

**The agent never reads the private vault.** Its retrieval corpus is a clone of
the *team* remote and nothing else — the same content any Soilytix employee can
already `git clone`. The private vault is a **build-time input** to the persona
bundle, and the bundle is never quoted back to a caller.

Why the bundle goes to Secret Manager rather than into the image or a repo: it
contains candid per-person commentary about the very people who can call the
agent. It is a sensitive asset. (This is a deliberate divergence from the
`kb-mcp` service, whose service account has *no* Secret Manager access because
its collection is baked into the image. Document it as an exception in the
Terraform stack the same way `kb-mcp` documents its no-IAP exception.)

## Commands

| command | plane | does |
|---|---|---|
| `ask-maurice build-persona` | build | compile the bundle from the private vault → `persona/bundle.json` (gitignored) |
| `ask-maurice publish-persona` | build | push that bundle to GCP Secret Manager as a new version |
| `ask-maurice corpus-sync` | runtime | clone/pull `Soilytix/vault` into `corpus/` |
| `ask-maurice bake-corpus` | image build | copy the retrievable subset of `corpus/` into `dist/corpus` for the image |
| `ask-maurice ask "…" --as julia@soilytix.com` | runtime | one question from the terminal, framed for that person |
| `ask-maurice serve` | runtime | HTTP service (Entra- or IAP-verified callers) |

## Local development

```bash
mise run setup          # toolchain, deps, hooks, then `check`
ask-maurice build-persona
ask-maurice corpus-sync
ask-maurice ask "why do we normalise by sequencing depth before the benchmark?" --as julia@soilytix.com
```

`build-persona` needs the private vault on disk (`ASK_MAURICE_PRIVATE_VAULT`).
Everything else runs off `corpus/` and a bundle — locally from `persona/bundle.json`,
in production from Secret Manager.

## Who the caller is: two access edges

`serve` accepts identity from either edge, and both can be on at once. Which one
a request used never changes the answer — both end at the same `Caller`, resolved
through the alias table baked into the persona bundle.

| edge | header | verification | typical caller |
|---|---|---|---|
| **Entra bearer** | `Authorization: Bearer …` | RS256, tenant JWKS, `iss`/`aud`/`tid` pinned | MCP client, script |
| **Google IAP** | `X-Goog-IAP-JWT-Assertion` | ES256, Google's JWK set, `iss`/`aud` pinned | browser, behind Cloud Run |

Nothing is shared between the two verifiers — different algorithm, different
keys, different issuer, different email claim — so they are separate functions
rather than one parameterised one.

Resolution order for a request is **verified bearer → verified IAP assertion →
anonymous**. Bearer wins because behind IAP *every* request carries an assertion,
so a bearer token on top of one is a deliberate act. Anonymous is development
only: with either edge configured a request that satisfies neither gets a 401,
and `RuntimeConfig.from_env` refuses to boot in production unless at least one is
configured. That guard matters more here than in a typical service — an
unidentified caller still gets an answer, but an unframed one, and the framing is
the product.

Set `ASK_MAURICE_IAP_AUDIENCE` to IAP's audience string for this exact service.
For Cloud Run that is
`/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME` — leading
slash, project *number* not project ID. Behind a load balancer IAP mints
`/projects/PROJECT_NUMBER/global/backendServices/SERVICE_ID` instead, so take
the value from the Terraform stack's output rather than assembling it by hand; a
mismatch is a 401 on every request.

## Building the container

Cloud Run, port 8080, non-root. The build machine is Maurice's laptop for now,
not CI — which is also why the corpus is baked by hand rather than cloned in a
build step.

```bash
ask-maurice corpus-sync    # refresh ./corpus from Soilytix/vault
mise run bake-corpus       # ./corpus -> ./dist/corpus (documents + COMMIT)
docker build -t ask-maurice .
```

**Why the bake step exists.** `COPY corpus/` would ship 2.7 GB (measured
2026-08-16: 1.2 GB `.git`, and a working tree that is mostly `lib/` binaries) to
deliver the 564 markdown files, 22.6 MB, that `Corpus.documents()` actually
returns. `bake-corpus` copies exactly that set — it calls `Corpus.documents()`
itself, so the baked set cannot drift from the retrievable set — and writes the
checkout's `git rev-parse HEAD` to a `COMMIT` file beside them.

That `COMMIT` file is not a convenience. Every `Excerpt.cite()` is
`path@commit[:8]`, so a corpus with no SHA is a corpus that cites its sources at
a commit nobody can look up. `Corpus` therefore accepts provenance from `.git`
(a dev checkout) *or* from `COMMIT` (a baked image), and raises `CorpusError`
when it has neither — an empty or whitespace-only `COMMIT` counts as neither.

The image build fails loudly if `dist/corpus` is missing, empty, or has no
`COMMIT`. A silently corpus-less image is worse than a failed build: it answers
every question with "retrieval found nothing" and looks like a model problem.

**The persona bundle is not in the image**, and there is a `RUN` check asserting
so. The image sets `ASK_MAURICE_BUNDLE_SOURCE=secret`; the bundle is fetched from
Secret Manager at boot and held in memory. `ASK_MAURICE_BUNDLE_SECRET` and the
access-edge variables come from the deployment.

## What this is not, yet

- **No literature/kb-mcp integration.** `runtime/literature.py` is a declared
  stub. Science questions are answered from the shared vault only; the agent is
  instructed to say when a claim needs a source it does not have.
- **No podcast or video generation.** The artifact router *classifies* and
  proposes; only the document loop produces anything. NotebookLM is not wired.
- **`transcripts/` is excluded from retrieval by default.** 66 transcript files
  sit on the team remote from before `transcripts/` entered `.kbignore`
  (graduated 2026-07-28, rule added 2026-07-30). They are shared, but they are
  pre-rule residue, not a deliberate include. Flip `ASK_MAURICE_INCLUDE_TRANSCRIPTS`
  once that call is made on purpose.
- **No Terraform here.** Hosting lives in the terraform repo, alongside
  `stacks/dev/kb-mcp`, following `docs/runbooks/host-internal-app.md`.
- **Slack ingress is not built.** `runtime/identity.py` resolves a Slack user ID
  to a person, but nothing listens on a Slack event API yet.
