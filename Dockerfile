# syntax=docker/dockerfile:1
#
# The runtime plane, as Cloud Run runs it.
#
# Two things are deliberately absent and both are load-bearing:
#
#   1. The persona bundle. It is built from .kbignore'd vault paths and carries
#      candid per-person commentary about the people who can call this service.
#      It travels build plane -> Secret Manager -> memory and never touches a
#      layer. `ASK_MAURICE_BUNDLE_SOURCE=secret` below is what makes the running
#      container fetch it at boot. See AGENTS.md § Sensitivity boundary, rule 2.
#
#   2. The corpus *checkout*. `COPY corpus/` would be 2.7 GB — 1.2 GB of `.git`
#      and a working tree that is mostly `lib/` binaries — to ship the 22.6 MB of
#      markdown retrieval actually reads. So `ask-maurice bake-corpus` runs first
#      and this copies its output instead. The build sequence is in the README.

ARG PYTHON_VERSION=3.13
# Matches the uv major pinned in mise.toml and the `required-version` in
# pyproject.toml, so the image resolves uv.lock the same way a laptop does.
ARG UV_VERSION=0.11.23
# Where `bake-corpus` wrote the documents + COMMIT file. Build context relative.
ARG BAKED_CORPUS=dist/corpus


# --- builder: uv.lock -> a self-contained venv -------------------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src
# README.md is not documentation here — pyproject's `readme` field points at it,
# so hatchling needs it present to build the wheel.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# --no-editable: the venv must not depend on /src, which the runtime stage drops.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


# --- runtime -----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG BAKED_CORPUS

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN useradd --system --create-home --uid 10001 app

COPY --from=builder /opt/venv /opt/venv

# Owned by root, read-only to the app user: nothing at runtime rewrites either.
WORKDIR /app
COPY ${BAKED_CORPUS}/ /app/corpus/

# A corpus-less image is not a broken build, it is a working service that answers
# every question with "retrieval found nothing" — which reads as a model problem
# for as long as it takes someone to check the image. Fail here instead.
RUN if [ ! -s /app/corpus/COMMIT ]; then \
        echo >&2 "FATAL: /app/corpus has no COMMIT file."; \
        echo >&2 "Run \`mise run bake-corpus\` before \`docker build\` — see README."; \
        exit 1; \
    fi; \
    if [ -z "$(find /app/corpus -name '*.md' -print -quit)" ]; then \
        echo >&2 "FATAL: /app/corpus contains no markdown. The bake produced nothing."; \
        exit 1; \
    fi; \
    echo "corpus: $(find /app/corpus -name '*.md' | wc -l) notes @ $(cat /app/corpus/COMMIT)"

# Second lock on rule 2. .dockerignore excludes persona/ and nothing above copies
# it, so this only fires if someone adds a COPY that should not exist.
RUN if find /app \( -name 'bundle.json' -o -name 'persona-bundle*.json' \) -print -quit \
        | grep -q .; then \
        echo >&2 "FATAL: a persona bundle is in the image. It is a sensitive asset and"; \
        echo >&2 "must reach the runtime through Secret Manager only. See AGENTS.md."; \
        exit 1; \
    fi

ENV ASK_MAURICE_CORPUS=/app/corpus \
    ASK_MAURICE_BUNDLE_SOURCE=secret \
    ASK_MAURICE_ENV=production
# ASK_MAURICE_BUNDLE_SECRET and the access-edge variables come from the
# deployment — the image has no idea which project or service it is running as.
# With ASK_MAURICE_ENV=production set here, a deploy that configures neither
# Slack nor Entra fails at boot rather than serving unframed answers to everyone.

USER app

# Cloud Run's default, and what `ask-maurice serve` already defaults to. $PORT is
# honoured because Cloud Run is entitled to pick another one.
EXPOSE 8080
CMD ["sh", "-c", "exec ask-maurice serve --host 0.0.0.0 --port ${PORT:-8080}"]
