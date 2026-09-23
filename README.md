<img src="brand/icon/icon-ask-maurice-128.png" align="left" width="128" hspace="16" alt="ask-maurice icon">

<h3>ask-maurice</h3>

<p>
  <sub>ASK THE VAULT, HEAR IT IN MY VOICE</sub>
  <br>
  <strong>Slack doppelgänger that answers in Maurice's voice, grounded in a shared team vault.</strong>
  <br>
  <br>
  <img src="https://img.shields.io/badge/python-%E2%89%A53.13-D78A7A?style=flat-square&amp;labelColor=2D2825" alt="Python 3.13+">
  <img src="https://img.shields.io/badge/surface-Slack-D78A7A?style=flat-square&amp;labelColor=2D2825" alt="Slack">
  <img src="https://img.shields.io/badge/runtime-Cloud%20Run-7E9688?style=flat-square&amp;labelColor=2D2825" alt="Cloud Run">
  <img src="https://img.shields.io/badge/retrieval-BM25%20%2B%20mixedbread-7E9688?style=flat-square&amp;labelColor=2D2825" alt="BM25 and mixedbread retrieval">
  <img src="https://img.shields.io/badge/persona-sensitive-A78D73?style=flat-square&amp;labelColor=2D2825" alt="Persona bundle is sensitive">
</p>

<br clear="left">

Someone on the team asks a question in Slack and gets an answer in Maurice's voice, grounded in the **shared** vault and framed for whoever asked.

1. **Persona is fixed, not prompted per call.** A build step compiles Maurice's person file, style notes and communication advice into one frozen bundle. Callers cannot change it.
2. **The asker is known, so the answer is framed.** Slack's user ID resolves via `users.info` to an email, then to a person file carrying that person's role and how the register should shift for them.
3. **Artifacts are proposed, not only produced on request.** An explainer question may warrant a written document, a podcast or a video; the agent decides and says so.

## Two planes

The load-bearing design decision. Read it before changing anything.

```
BUILD PLANE (Maurice's machine only, never hosted)
  private vault
    people/*.md                 ← .kbignore'd, never reaches the team remote
    me/*.md
        │
        │  ask-maurice build-persona
        ▼
    persona bundle (JSON)  ── ask-maurice publish-persona ──▶  GCP Secret Manager
                                                                       │
────────────────────────────────────────────────────────────────────── │ ────────
RUNTIME PLANE (Cloud Run; Slack signature at the edge)                 │
                                                                       ▼
    baked corpus (team vault @ main)                          persona bundle
    markdown files + COMMIT, baked into the image             loaded at boot,
        │                                                     held in memory only
        └──── retrieval ────▶  agent  ◀──── framing for the caller ────┘
                                 │
                                 ▼
                          answer (+ optional artifact)
```

**The agent never reads the private vault.** Its corpus is a clone of the team remote, the same content anyone on the team can already `git clone`. The private vault only feeds the persona bundle, which contains candid per-person commentary: it lives in Secret Manager, never in git or the image, and every answer is shingle-checked against it before it is sent.

**Two corpora, kept apart.** The vault is Maurice's own writing, asserted in the first person, retrieved by local BM25 (optionally fused with a mixedbread index). The literature is a mixedbread collection of papers the team has read; it is a tool the model reaches for, and what it finds is attributed, never asserted.

## Commands

| command | plane | does |
|---|---|---|
| `ask-maurice build-persona` | build | compile the bundle from the private vault → `persona/bundle.json` (gitignored) |
| `ask-maurice publish-persona` | build | push that bundle to Secret Manager as a new version |
| `ask-maurice corpus-sync` | runtime | clone/pull `ASK_MAURICE_CORPUS_REMOTE` into `corpus/` |
| `ask-maurice vault-index` | runtime | push that checkout into the mixedbread vault store (opt-in; asks first) |
| `ask-maurice bake-corpus` | image build | copy the retrievable subset of `corpus/` plus its `COMMIT` into `dist/corpus` |
| `ask-maurice ask "…" --as alex@example.com` | runtime | one question from the terminal, framed for that person |
| `ask-maurice serve` | runtime | HTTP service: the Slack slash command, plus `/ask` for local use |

## Local development

```bash
mise run setup          # toolchain, deps, hooks, then `check`
cp .env.example .env.local   # set ASK_MAURICE_PRIVATE_VAULT and ASK_MAURICE_CORPUS_REMOTE
ask-maurice build-persona
ask-maurice corpus-sync
ask-maurice ask "why do we normalise by sequencing depth?" --as alex@example.com
```

For the first deploy (secret, bundle, image, Slack app, Cloud Run), see [`RUNBOOK.md`](RUNBOOK.md).

## Access and trust

Slack is the only door. `/slack/command` verifies Slack's HMAC signature and takes the caller from the payload's `user_id`; production refuses to boot without it. `/ask` answers only when nothing can identify anybody, i.e. a local `serve`, and 401s once Slack is configured. So who can ask is a Slack workspace and channel decision, and content exposure stays bounded by the shared vault.

Answers post `in_channel` and quote the question; the holding ack and failures stay ephemeral. Slack wants a reply within three seconds, so the route acks and a background task posts the answer, which needs CPU allocated outside a request on Cloud Run.

## What this is not, yet

- **The vault store is not turned on.** Indexing the shared vault into a third-party service is a data decision; `vault-index` asks first and nothing runs it automatically.
- **Nothing keeps the corpus fresh.** Re-running `corpus-sync`, `bake-corpus` and a deploy is manual.
- **No podcast or video generation.** The artifact router proposes; only documents are produced.
- **`transcripts/` is excluded from retrieval by default**, until `ASK_MAURICE_INCLUDE_TRANSCRIPTS` is set on purpose.
- **Slash command only.** There is no Events API route, so mentions and DMs do not reach the agent.
