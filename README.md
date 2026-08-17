# ask-maurice

A hosted doppelgänger agent. Someone on the Soilytix team asks a question in
Slack and gets an answer in Maurice's voice, grounded in the **shared** Soilytix
vault, framed for whoever asked.

Three things make it more than a wrapper around a chat model:

1. **Persona is fixed, not prompted per call.** A build step compiles Maurice's
   own person file, style notes and the communication-advisor dict into one
   frozen bundle. Callers cannot change it.
2. **The asker is known, so the answer is framed.** Slack tells us the user ID,
   which `users.info` turns into an email, which resolves to a person file in the
   vault — carrying that person's role, what they care about, and how Maurice's
   default register should shift for them.
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
RUNTIME PLANE (Cloud Run; Slack signature at the edge)                 │
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

## Retrieval

Two corpora, and keeping them apart is the point.

**The vault** is Maurice's own writing, so the agent speaks for it in the first
person. It is retrieved before every call, by default with the local BM25 scan
over `corpus/`. Set `ASK_MAURICE_VAULT_STORE` and `ASK_MAURICE_VAULT_RETRIEVAL=hybrid`
and the same notes are also indexed in mixedbread and the two rankings are fused:
BM25 is what finds `in-toto` in the twenty-two notes that name it, embeddings are
what finds the note that answers the question without ever using the asker's word.
Provenance survives the round trip because the indexer writes the repo path and
the indexed commit into each file's metadata, so a mixedbread hit rebuilds the
same `path@commit` citation a local excerpt carries.

**The literature** is the research collection in mixedbread — papers the team has
actually read and kept, largely out of Norman's Zotero library. It is third-party
evidence, so it is a *tool* the model reaches for when an answer turns on an
external finding, not something folded into every request. A paper gets
attributed; a vault note gets asserted. `/ask` returns them in separate fields
for the same reason.

Set `MXBAI_API_KEY` and neither store, and none of this exists: no tool, no
semantic index, lexical retrieval only. Set a store without the key and the
config refuses to load — a retrieval path that is configured and dead is worse
than one that is absent, because the agent cannot tell "no result" from "not
connected".

## Commands

| command | plane | does |
|---|---|---|
| `ask-maurice build-persona` | build | compile the bundle from the private vault → `persona/bundle.json` (gitignored) |
| `ask-maurice publish-persona` | build | push that bundle to GCP Secret Manager as a new version |
| `ask-maurice corpus-sync` | runtime | clone/pull `Soilytix/vault` into `corpus/` |
| `ask-maurice vault-index` | runtime | push that checkout into the mixedbread vault store (opt-in; asks first) |
| `ask-maurice bake-corpus` | image build | copy the retrievable subset of `corpus/` into `dist/corpus` for the image |
| `ask-maurice ask "…" --as julia@soilytix.com` | runtime | one question from the terminal, framed for that person |
| `ask-maurice serve` | runtime | HTTP service: the Slack slash command, plus `/ask` for local use |

## Local development

For the full first-deploy sequence — secret, bundle, image, Slack app, Cloud Run,
in the order an operator has to type them — see **[`RUNBOOK.md`](RUNBOOK.md)**.
What follows is just the local loop.

```bash
mise run setup          # toolchain, deps, hooks, then `check`
ask-maurice build-persona
ask-maurice corpus-sync
ask-maurice ask "why do we normalise by sequencing depth before the benchmark?" --as julia@soilytix.com

ask-maurice vault-index --dry-run    # what would be sent to mixedbread
ask-maurice vault-index              # send it, after confirming
```

`build-persona` needs the private vault on disk (`ASK_MAURICE_PRIVATE_VAULT`).
Everything else runs off `corpus/` and a bundle — locally from `persona/bundle.json`,
in production from Secret Manager.

## Who the caller is: one access edge

| route | who gets through | proves | identity from |
|---|---|---|---|
| `/slack/command` | the team | Slack sent it (HMAC-SHA256, 5-min window) | `user_id` in the payload → `users.info` |
| `/ask` | nobody on a deployed service; anyone on a local `serve` | nothing — there is no verifier | `--as`, or anonymous |
| `/healthz` | anyone | nothing | n/a |

`RuntimeConfig.from_env` refuses to boot in production without the Slack edge
configured. That guard matters more here than in a typical service — an
unidentified caller still gets an answer, but an unframed one, and the framing is
the product.

`/ask` has no edge of its own and answers only when *nothing* on the deployment
can identify anybody, which in practice means a local `serve`. As soon as Slack
is configured it 401s. That is deliberate rather than an oversight: a route
handing unframed answers to whoever finds the URL is not a smaller version of
this product, it is a different one.

### Why there is only one edge

Two others existed and both were removed rather than left switched off.

**Google IAP** fronts the *whole* Cloud Run service, so it also intercepts
`/slack/command` — and Slack cannot sign in to it. The two were exclusive in
practice, and Slack is the surface the team actually uses. Running both would
mean a load balancer with a URL map routing `/slack/command` to an IAP-free
backend.

**Entra bearer tokens** worked and conflicted with nothing. They were removed for
a duller reason: nothing on the first surface used them. `/ask` served an MCP
client that is not wired and scripts nobody had written, at the cost of a config
class, a verifier, a JWKS round trip, an RFC 9728 discovery route and the PyJWT
dependency. That is a lot of standing surface for a caller who does not exist
yet, so it went, and comes back when there is something to authenticate.

Both verifiers are in the git history rather than deleted knowledge — `verify_iap`
(ES256 against Google's static JWK set) and `verify` (RS256 against the tenant
JWKS, with the `tid` check) — and are worth restoring rather than rewriting.

What this costs, plainly: **there is no authenticated HTTP way in at all.** Slack
is the product's only door. Anything that wants to ask over HTTP — an MCP server,
a scheduled job, a script — needs an edge built for it first.

### Slack is a different shape of trust, deliberately

Worth stating plainly rather than letting the word "auth" cover it. A signed
assertion from an identity provider names the human who signed in. Slack instead
hands us one shared secret proving *Slack* sent the request, plus a `user_id` in
the body that we take on Slack's word. Transport authentication and caller
identity come apart — and with Entra gone, this is the only trust model left, so
it is worth understanding rather than skimming.

Two things bound the identity risk: Slack sets `user_id` server-side, so a
workspace member cannot forge another's, and an unresolved caller falls back to
no framing rather than to a guess. **Authorisation** is workspace and channel
membership, administered in Slack rather than in the stack — so who can ask is a
Slack admin decision, not an IAM one. Content exposure stays bounded either way,
since retrieval reads only the shared vault that any Soilytix employee can
already clone.

Slack is also the only route that answers asynchronously. Slack wants HTTP 200
within three seconds and an `opus-5` answer at `xhigh` effort is nowhere near
that, so the route acks immediately and a background task posts to the payload's
`response_url` when it finishes. **That requires CPU allocated outside a request
on Cloud Run** — with the default throttling the background task stalls the
moment the ack returns, and the asker never hears back.

### Ephemeral ack, public answer

The holding reply is ephemeral — a "thinking…" in everyone's channel is pure
clutter — and the answer itself goes `in_channel`, because the team reading each
other's answers is most of the value. Failures stay ephemeral: ":warning: rate
limited by the Anthropic API" is noise for everyone except the person waiting.
`runtime/server.py:slack_reply` owns that split.

One consequence of the pairing, and the reason a public answer quotes the
question: an ephemeral *first* response means Slack never echoes the invocation
into the channel, so an unquoted answer would land with nothing saying what was
asked.

What going public does and does not expose is worth being precise about, because
the framing draws on `.kbignore`'d material:

- **Vault excerpts are not a new disclosure.** Retrieval reads only the shared
  vault, which every Soilytix employee can already clone, and citations are
  `path@commit` into it.
- **Persona-bundle content was never in the answer to begin with.** `redaction.py`
  shingle-checks every answer against the bundle and scrubs a hit before it is
  sent, and the prompt carries a refusal instruction on top. That guard is
  channel-independent; it did not become load-bearing because of this change.
- **What is new is the framing itself.** Everyone in the channel now sees that an
  answer to one person is pitched differently than to another. Nothing quotes the
  commentary behind that, but the shaping is legible, and per-person register is
  now a thing colleagues can compare. That is a disclosure surface the persona
  bundle was not built for, and it is the part worth having decided on purpose.

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
Secret Manager at boot and held in memory.

The deployment must supply three things the image cannot know: `ANTHROPIC_API_KEY`,
`ASK_MAURICE_BUNDLE_SECRET`, and at least one access edge. All three are checked
at boot, so a container missing any of them refuses to start rather than serving
broken answers — `Agent.build` will not construct a client without a key, since
the SDK would otherwise defer the failure to the first question and surface it as
a 500 that reads like a model problem.

If the Slack edge is on, the service also needs **CPU allocated outside a
request**, for the reason in the access-edge section above. That is a Cloud Run
setting, not an image one, and it changes the billing model from per-request to
instance-based.

## What this is not, yet

- **The vault store is not turned on.** The code path, the indexer and the
  fusion are here and tested, but pushing the shared vault to a third-party
  index is a data decision, not a deployment detail. `vault-index` asks before
  it uploads and nothing runs it automatically. Until someone makes that call
  deliberately, `ASK_MAURICE_VAULT_RETRIEVAL` stays `local`.
- **kb-mcp is still not wired.** The literature path now goes to mixedbread
  instead, which covers the papers but not the rest of what kb-mcp serves. The
  agent is still instructed to say when a claim needs a source it does not have.
- **Nothing keeps the vault store fresh.** `vault-index` is idempotent and skips
  unchanged notes by content hash, so it is cheap to re-run after
  `corpus-sync` — but scheduling that pair is a hosting concern and lives in the
  terraform repo, not here.
- **No podcast or video generation.** The artifact router *classifies* and
  proposes; only the document loop produces anything. NotebookLM is not wired.
- **`transcripts/` is excluded from retrieval by default.** 66 transcript files
  sit on the team remote from before `transcripts/` entered `.kbignore`
  (graduated 2026-07-28, rule added 2026-07-30). They are shared, but they are
  pre-rule residue, not a deliberate include. Flip `ASK_MAURICE_INCLUDE_TRANSCRIPTS`
  once that call is made on purpose.
- **No Terraform here.** Hosting lives in the terraform repo, alongside
  `stacks/dev/kb-mcp`, following `docs/runbooks/host-internal-app.md`.
- **Slack is slash-command only.** `/slack/command` is built and verified; there
  is no Events API route, so app mentions and DMs do not reach the agent. Adding
  one means handling Slack's retry/dedupe semantics, which slash commands do not
  have.
