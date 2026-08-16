"""Typer entry points for both planes.

`build-persona` and `publish-persona` are build plane — they read the private
vault and must only ever run on Maurice's machine. Everything else is runtime
plane and touches nothing but the shared-vault clone and a compiled bundle.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown

from ask_maurice.config import BuildConfig, ConfigError, RuntimeConfig

app = typer.Typer(help="Maurice's doppelgänger: shared-vault answers, framed for who asked.")
console = Console()
err = Console(stderr=True)


def _fail(message: str) -> None:
    err.print(f"[red]error[/red] {message}")
    raise typer.Exit(1)


@app.command("build-persona")
def build_persona() -> None:
    """BUILD PLANE: compile the persona bundle from the private vault."""
    from ask_maurice.build.compile import BuildError, compile_bundle, unjoined, write_bundle

    try:
        config = BuildConfig.from_env()
        bundle = compile_bundle(config.private_vault)
        path = write_bundle(bundle, config.bundle_path)
    except (ConfigError, BuildError) as exc:
        _fail(str(exc))
        return

    console.print(f"[green]built[/green] {path} (vault @ {bundle.source_commit})")
    console.print(f"  participants: {len(bundle.participants)}")
    console.print(f"  aliases:      {len(bundle.aliases)}")
    console.print(f"  sources:      {', '.join(bundle.built_from)}")
    if missing := unjoined(bundle):
        err.print(
            f"[yellow]warning[/yellow] no email found for {', '.join(missing)} — they will get "
            "no framing when asking over Slack or Entra. Check their person file's aliases."
        )
    console.print(
        "\n[yellow]This file is a sensitive asset.[/yellow] It is gitignored and mode 0600. "
        "Publish it with `publish-persona`; never commit it, never copy it into an image."
    )


@app.command("publish-persona")
def publish_persona(
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt"),
) -> None:
    """BUILD PLANE: push the local bundle to GCP Secret Manager as a new version."""
    import json

    from ask_maurice.build.publish import publish
    from ask_maurice.persona import PersonaBundle

    try:
        config = BuildConfig.from_env()
    except ConfigError as exc:
        _fail(str(exc))
        return
    if not config.secret_name:
        _fail("ASK_MAURICE_BUNDLE_SECRET is not set")
    if not config.bundle_path.is_file():
        _fail(f"no bundle at {config.bundle_path} — run `build-persona` first")

    bundle = PersonaBundle.from_dict(json.loads(config.bundle_path.read_text(encoding="utf-8")))
    if not yes and not typer.confirm(
        f"publish {len(bundle.participants)} participants to {config.secret_name}?"
    ):
        raise typer.Abort
    console.print(f"[green]published[/green] {publish(bundle, config.secret_name)}")


@app.command("corpus-sync")
def corpus_sync() -> None:
    """RUNTIME PLANE: clone or fast-forward the shared-vault checkout."""
    from ask_maurice.runtime.corpus import Corpus, CorpusError, sync

    try:
        config = RuntimeConfig.from_env()
        head = sync(config.corpus_path, config.corpus_remote, config.corpus_ref)
    except (ConfigError, CorpusError) as exc:
        _fail(str(exc))
        return
    corpus = Corpus(root=config.corpus_path, include_transcripts=config.include_transcripts)
    console.print(f"[green]synced[/green] {config.corpus_path} @ {head[:8]}")
    console.print(f"  retrievable notes: {len(corpus.documents())}")
    if not config.include_transcripts:
        console.print("  [dim]transcripts/ excluded (ASK_MAURICE_INCLUDE_TRANSCRIPTS)[/dim]")


@app.command("bake-corpus")
def bake_corpus(
    out: Path = typer.Option(  # noqa: B008 - Typer reads the default as the option spec
        Path("dist/corpus"), "--out", help="directory the image will COPY from"
    ),
) -> None:
    """IMAGE BUILD: copy the retrievable subset of the corpus checkout into `--out`.

    Only the documents `Corpus.documents()` would return, plus a COMMIT file
    carrying the checkout's HEAD — that pair is a complete corpus as far as the
    runtime is concerned, and it is ~1% of the checkout's size.
    """
    from ask_maurice.bake import BakeError, bake_from

    try:
        config = RuntimeConfig.from_env()
        result = bake_from(config.corpus_path, out, include_transcripts=config.include_transcripts)
    except (ConfigError, BakeError) as exc:
        _fail(str(exc))
        return

    console.print(f"[green]baked[/green] {result.out} @ {result.commit[:8]}")
    console.print(f"  documents: {result.documents}")
    console.print(f"  size:      {result.bytes_copied / 1_000_000:.1f} MB")


@app.command("ask")
def ask(
    question: str = typer.Argument(..., help="the question"),
    as_handle: str = typer.Option("", "--as", help="email, alias or name of the asker"),
) -> None:
    """RUNTIME PLANE: one question from the terminal."""
    from ask_maurice.runtime import bundle as bundle_mod
    from ask_maurice.runtime import redaction
    from ask_maurice.runtime.agent import Agent, AgentError
    from ask_maurice.runtime.corpus import Corpus, CorpusError
    from ask_maurice.runtime.identity import Caller, from_handle

    try:
        config = RuntimeConfig.from_env()
        persona = bundle_mod.load(config)
    except (ConfigError, bundle_mod.BundleError) as exc:
        _fail(str(exc))
        return

    redaction.install(redaction.Redactor(persona))
    corpus = Corpus(root=config.corpus_path, include_transcripts=config.include_transcripts)
    caller = from_handle(as_handle, persona) if as_handle else Caller(handle="anonymous")
    if as_handle and not caller.known:
        err.print(f"[yellow]note[/yellow] {as_handle} is not in the bundle — answering unframed")

    try:
        answer = Agent.build(persona, corpus).answer(question, caller)
    except (AgentError, CorpusError) as exc:
        _fail(str(exc))
        return

    console.print(Markdown(answer.text))
    if answer.sources:
        console.print(f"\n[dim]sources: {', '.join(answer.sources)}[/dim]")
    if answer.suggestion.kind.value != "none":
        availability = "" if answer.suggestion.available else " (not wired up yet)"
        console.print(
            f"[dim]artifact candidate: {answer.suggestion.kind.value}{availability}[/dim]"
        )


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8080, help="bind port"),
) -> None:
    """RUNTIME PLANE: serve the HTTP API."""
    import uvicorn

    from ask_maurice.runtime.server import create_app

    try:
        config = RuntimeConfig.from_env()
    except ConfigError as exc:
        _fail(str(exc))
        return
    if not config.has_access_edge:
        err.print(
            "[yellow]warning[/yellow] no access edge configured (Entra bearer or IAP) — "
            "every caller is anonymous and every answer unframed"
        )
    # log_config=None keeps uvicorn from replacing the root logger's handlers,
    # which is where the redaction filter lives.
    uvicorn.run(create_app(config), host=host, port=port, log_config=None)


if __name__ == "__main__":
    app()
