# Runbook: standing ask-maurice up

The exact order, first deploy to first answer. `README.md` says what the service
is and why; this says what to type.

Two things shape the sequence and neither is negotiable:

- **The build plane runs on Maurice's machine only.** Steps 1–3 read the private
  vault. They never run in CI, in a container, or on anyone else's laptop.
- **The container refuses to boot half-configured.** The image sets
  `ASK_MAURICE_ENV=production`, so a deploy with no access edge, no
  `ANTHROPIC_API_KEY`, or no reachable bundle secret fails at startup rather than
  serving unframed or source-less answers. That is why the secret and the Slack
  app come *before* the deploy, not after.

## The shape this deploys

Slack in front, `--allow-unauthenticated` at the Cloud Run layer, and the app
enforcing the edge behind that:

| route | who gets through | how |
|---|---|---|
| `/slack/command` | the team | HMAC-SHA256 signature, five-minute replay window |
| `/ask` | nobody | 401 — it answers only when no edge is configured at all, i.e. a local `serve` |
| `/healthz` | anyone | it returns a status and the persona commit, nothing else |

`--allow-unauthenticated` is not a gap here: Slack has to reach the service from
the public internet, and the slash command's signature is the real gate. Nothing
else on the service answers a stranger.

**The slash command is the only way in.** The Entra bearer and Google IAP edges
both existed and both were removed — IAP because it fronts the whole service and
would intercept `/slack/command`, Entra because nothing on this surface used it.
See README § Why there is only one edge. Two consequences for you as operator:

- There is no browser or script access to `/ask`. Do not plan a scheduled job or
  an MCP client against this deployment until an edge exists for it.
- There is nothing to configure here. No audiences, no tenant ids, no JWKS. If a
  previous deploy set `ASK_MAURICE_ENTRA_*` or `ASK_MAURICE_IAP_AUDIENCE`, delete
  them — they are read by nothing, and leaving them makes the console look like
  auth is configured when it is not.

Steps 6–8 belong in the terraform repo long-term (alongside `stacks/dev/kb-mcp`,
per `docs/runbooks/host-internal-app.md`). What follows is the manual equivalent
for the first deploy, and it is what the stack should end up expressing.

---

## Before you touch anything: collect these

| value | where it comes from |
|---|---|
| GCP project id **and project number** | `gcloud projects describe PROJECT_ID` — the bundle secret's resource name uses the number, not the id |
| Anthropic API key | console.anthropic.com |
| Read access to `Soilytix/vault` | your own GitHub SSH key; the clone is a plain `git clone` |
| Private vault on disk | `morris-frank/vault`, cloned locally |
| mixedbread API key *(optional)* | platform.mixedbread.com — omit it and the whole integration is simply off |
| mixedbread papers store name *(optional)* | the research collection; create it in the mixedbread dashboard first, this code does not create stores |
| Slack workspace admin | to create the app in step 5 |

Nothing here is a placeholder you can guess later. A wrong project *number* in the
bundle secret's resource name is a container that cannot boot; an absent store
name with a key set is a config that refuses to load, on purpose.

---

## 1. Toolchain (build plane, once per machine)

```bash
curl https://mise.run | sh          # if mise is not already installed
git clone git@github.com:morris-frank/ask-maurice.git
cd ask-maurice
mise run setup                      # installs the pinned toolchain, syncs deps,
                                    # installs git hooks, then runs `check`
```

`mise run setup` ends green or you do not continue. Everything below assumes the
pinned toolchain, which is why each command goes through `mise run` or
`mise exec --` rather than a bare binary.

Then write `.env.local` (gitignored, auto-loaded by mise) from `.env.example`:

```bash
cp .env.example .env.local
$EDITOR .env.local
```

Minimum for steps 2–4: `ASK_MAURICE_PRIVATE_VAULT`, `ANTHROPIC_API_KEY`, and
`ASK_MAURICE_BUNDLE_SECRET` once step 2 has created it.

## 2. Create the persona secret (GCP, once)

`publish-persona` adds a *version*; it does not create the secret. Create it
first, and grant the two roles that the two planes need — they are deliberately
different.

```bash
export PROJECT_ID=your-project
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
export REGION=europe-west3
export RUNTIME_SA=ask-maurice@${PROJECT_ID}.iam.gserviceaccount.com

gcloud secrets create ask-maurice-persona \
  --project="$PROJECT_ID" --replication-policy=automatic

# Build plane: Maurice's own account may add versions.
gcloud secrets add-iam-policy-binding ask-maurice-persona \
  --project="$PROJECT_ID" \
  --member="user:maurice@soilytix.com" \
  --role=roles/secretmanager.secretVersionAdder

# Runtime: the service account may read, and only this one secret.
gcloud iam service-accounts create ask-maurice --project="$PROJECT_ID"
gcloud secrets add-iam-policy-binding ask-maurice-persona \
  --project="$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role=roles/secretmanager.secretAccessor
```

That accessor binding is a documented divergence from the `kb-mcp` stack, whose
service account has no Secret Manager access at all. Note it in the stack README
the way kb-mcp notes its no-IAP exception.

Put the resulting name in `.env.local`:

```
ASK_MAURICE_BUNDLE_SECRET=projects/<PROJECT_NUMBER>/secrets/ask-maurice-persona/versions/latest
```

## 3. Compile and publish the persona bundle (build plane)

```bash
mise run build-persona                                  # private vault -> persona/bundle.json
mise exec -- uv run ask-maurice publish-persona         # -> Secret Manager, asks first
```

`build-persona` warns about any participant it could not join to an email. Those
people get *no framing* when they use the slash command — fix their person file's
aliases and rebuild rather than shipping the gap.

The file it writes is a sensitive asset: gitignored, mode 0600, a pre-commit hook
refuses it, and the Dockerfile has a `RUN` check asserting it never reaches a
layer. It goes to Secret Manager and nowhere else.

## 4. Corpus, smoke test, and the image

```bash
mise exec -- uv run ask-maurice corpus-sync             # Soilytix/vault -> ./corpus
mise exec -- uv run ask-maurice ask \
  "why do we normalise by sequencing depth before the benchmark?" \
  --as julia@soilytix.com
```

An answer with `path@commit` sources means both planes work locally. Only then
build the image:

```bash
mise run bake-corpus                                    # ./corpus -> ./dist/corpus
gcloud auth configure-docker ${REGION}-docker.pkg.dev
gcloud artifacts repositories create ask-maurice \
  --project="$PROJECT_ID" --location="$REGION" --repository-format=docker

export IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/ask-maurice/ask-maurice:$(date +%Y%m%d-%H%M)
docker build --platform linux/amd64 -t "$IMAGE" .       # --platform matters on an arm Mac
docker push "$IMAGE"
```

`bake-corpus` before `docker build`, always. The build fails loudly if
`dist/corpus` is missing, empty, or has no `COMMIT` — a corpus-less image is a
service that answers every question with "retrieval found nothing" and reads like
a model problem for as long as it takes someone to check.

## 5. Create the Slack app (Slack admin, before the deploy)

Both Slack values must exist before the deploy. A signing secret without a bot
token is not an access edge and the config rejects it, and with no other edge
left there is nothing for the service to fall back to — it will not boot. In
api.slack.com/apps → **Create New App** → *From scratch*:

1. **Basic Information → Signing Secret** → this is `ASK_MAURICE_SLACK_SIGNING_SECRET`.
2. **OAuth & Permissions → Bot Token Scopes** → add `users:read.email`, and
   nothing else. The delayed answer posts to the payload's pre-signed
   `response_url`, which needs no scope.
3. **Install to Workspace** → the `xoxb-…` bot token is `SLACK_BOT_TOKEN`.
4. **Slash Commands → Create New Command** → e.g. `/maurice`. Leave the Request
   URL as a placeholder for now; step 7 fills in the real one.

Store both in Secret Manager rather than as plain env vars:

```bash
printf %s 'THE-SIGNING-SECRET' | gcloud secrets create ask-maurice-slack-signing \
  --project="$PROJECT_ID" --replication-policy=automatic --data-file=-
printf %s 'xoxb-THE-BOT-TOKEN' | gcloud secrets create ask-maurice-slack-bot \
  --project="$PROJECT_ID" --replication-policy=automatic --data-file=-
printf %s 'sk-ant-THE-KEY' | gcloud secrets create ask-maurice-anthropic \
  --project="$PROJECT_ID" --replication-policy=automatic --data-file=-

for s in ask-maurice-slack-signing ask-maurice-slack-bot ask-maurice-anthropic; do
  gcloud secrets add-iam-policy-binding "$s" --project="$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor
done
```

## 6. Deploy to Cloud Run

```bash
gcloud run deploy ask-maurice \
  --project="$PROJECT_ID" --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$RUNTIME_SA" \
  --port=8080 \
  --allow-unauthenticated \
  --no-cpu-throttling \
  --min-instances=0 --max-instances=4 \
  --timeout=300 \
  --set-env-vars="ASK_MAURICE_BUNDLE_SECRET=projects/${PROJECT_NUMBER}/secrets/ask-maurice-persona/versions/latest" \
  --set-secrets="ANTHROPIC_API_KEY=ask-maurice-anthropic:latest,\
ASK_MAURICE_SLACK_SIGNING_SECRET=ask-maurice-slack-signing:latest,\
SLACK_BOT_TOKEN=ask-maurice-slack-bot:latest"
```

`--no-cpu-throttling` is **not** a performance preference. Slack wants HTTP 200
inside three seconds; an `opus-5` answer at `xhigh` effort is nowhere near that,
so the route acks immediately and a background task posts the answer to
`response_url`. With default throttling that task stalls the moment the ack
returns and the asker never hears back. It changes billing from per-request to
instance-based — that is the price of the async answer.

If the deploy's first revision fails to become ready, the boot guards did their
job. Read the revision logs: they name the missing variable.

## 7. Point Slack at the service, and verify

```bash
gcloud run services describe ask-maurice \
  --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)'
```

Back in the Slack app → **Slash Commands** → set the Request URL to
`https://SERVICE_URL/slack/command` → Save.

Then, in Slack, from an account whose email is in the bundle:

```
/maurice why do we normalise by sequencing depth before the benchmark?
```

You should see an ephemeral ack within a second, then the real answer **posted
into the channel** shortly after, quoting the question. Do this in a channel you
do not mind writing to — the answer is public to everyone in it, by design; see
README § Ephemeral ack, public answer for what that does and does not expose.

Two failure modes worth telling apart:

- **Ack but no answer** → CPU throttling, or the background task raised. Check the
  Cloud Run logs for `slack answer failed`.
- **`invalid slack request`** → signing secret mismatch, or a clock more than five
  minutes off. The log line says which; the caller is told only that it was
  rejected.

Logs never carry the question, the answer, the framing, or the bundle. Caller
handle and whether they resolved is the whole of it, by design.

## 8. Optional: the mixedbread literature store

The papers collection is off unless configured, and off is a working service —
just one that answers from the vault alone.

```bash
printf %s 'THE-MXBAI-KEY' | gcloud secrets create ask-maurice-mxbai \
  --project="$PROJECT_ID" --replication-policy=automatic --data-file=-
gcloud secrets add-iam-policy-binding ask-maurice-mxbai --project="$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor

gcloud run services update ask-maurice \
  --project="$PROJECT_ID" --region="$REGION" \
  --update-secrets="MXBAI_API_KEY=ask-maurice-mxbai:latest" \
  --update-env-vars="ASK_MAURICE_LITERATURE_STORE=soilytix-papers"
```

The store must already exist in mixedbread; this code searches stores, it does
not create them. A store name without a key — or a key with a store that does not
exist — is a configured, dead retrieval path, so the config refuses to load
instead. On boot the logs say plainly which of the two states you are in.

---

## Deliberately not in the sequence

These are decisions, not steps. Doing them because a runbook listed them is the
failure this section exists to prevent.

- **`vault-index` — sending the shared vault to mixedbread.** The indexer, the
  fusion and the provenance round trip are built and tested. Team-vault content
  being readable by everyone at Soilytix is what makes uploading it *possible*;
  it is not what makes it *decided*. When someone makes that call on purpose:

  ```bash
  mise exec -- uv run ask-maurice vault-index --dry-run   # exactly what would be sent
  mise exec -- uv run ask-maurice vault-index             # sends it, after confirming
  ```

  then set `ASK_MAURICE_VAULT_STORE` and `ASK_MAURICE_VAULT_RETRIEVAL=hybrid` on
  the service. Until then it stays `local`. Nothing schedules it. The persona
  bundle is never in scope for this — no store, no index, no exception.
- **`ASK_MAURICE_INCLUDE_TRANSCRIPTS`.** 66 transcript files predate
  `transcripts/` entering `.kbignore`. They are shared, but they are pre-rule
  residue rather than a deliberate include.
- **Any authenticated HTTP surface at all.** `/ask` authenticates nobody now that
  both bearer edges are gone. Bringing one back is a deliberate piece of work —
  an Entra verifier again (config, JWKS, the RFC 9728 route, PyJWT), or a load
  balancer with a URL map routing `/slack/command` to an IAP-free backend. Decide
  it when something actually needs to ask over HTTP, not before.

## Keeping it running

The vault moves; the image does not. Refreshing retrieval is a rebuild, and it
is four commands:

```bash
mise exec -- uv run ask-maurice corpus-sync
mise run bake-corpus
docker build --platform linux/amd64 -t "$IMAGE" . && docker push "$IMAGE"
gcloud run deploy ask-maurice --project="$PROJECT_ID" --region="$REGION" --image="$IMAGE"
```

Nothing automates that yet — scheduling it is a hosting concern and belongs in
the terraform repo. Until it exists, every citation the service emits is
`path@commit` for the commit that was baked, which is at least honest about how
stale it is.

Re-publish the persona bundle whenever a person file or the advisor dict changes:

```bash
mise run build-persona && mise exec -- uv run ask-maurice publish-persona
```

The running service reads the bundle at boot, so a new secret version needs a new
revision to take effect:

```bash
gcloud run services update ask-maurice --project="$PROJECT_ID" --region="$REGION"
```
