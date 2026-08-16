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
RUNTIME PLANE (Cloud Run, Entra-gated)                                 │
                                                                       ▼
    shared vault clone (team: Soilytix/vault @ main)          persona bundle
    577 markdown files, scheduled pull                        loaded at boot,
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
| `ask-maurice ask "…" --as julia@soilytix.com` | runtime | one question from the terminal, framed for that person |
| `ask-maurice serve` | runtime | HTTP service (Entra-verified callers) |

## Local development

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
- **Slack ingress is not built.** `runtime/identity.py` resolves a Slack user ID
  to a person, but nothing listens on a Slack event API yet.
