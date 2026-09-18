"""KnowMemo command-line interface (design doc §30).

Commands that belong to a later milestone raise
:class:`~knowmemo.errors.NotImplementedYet` with the milestone that will
deliver them. They never silently succeed and never pretend to have run — the
distinction between "not built yet" and "broken" is load-bearing here.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from knowmemo import __version__
from knowmemo.config.settings import ConfigError, Settings, load_settings
from knowmemo.domain.source import SourceStatus
from knowmemo.errors import KnowMemoError
from knowmemo.health import CheckStatus, run_doctor
from knowmemo.ingestion.pipeline import IngestionPipeline, IngestionReport, build_connector
from knowmemo.logging_setup import configure_logging
from knowmemo.storage.database import (
    create_db_engine,
    data_root,
    init_db,
    make_session_factory,
    session_scope,
)
from knowmemo.storage.repositories import (
    ImportRunRepository,
    RecordStateRepository,
    SegmentRepository,
    SourceRepository,
)

app = typer.Typer(
    name="knowmemo",
    help="KnowMemo — a local-first personal memory and knowledge system.",
    no_args_is_help=True,
    add_completion=False,
)
source_app = typer.Typer(name="source", help="Configure and inspect data sources.")
import_app = typer.Typer(name="import", help="Run and inspect ingestion.")
config_app = typer.Typer(name="config", help="Inspect resolved configuration.")
app.add_typer(source_app, name="source")
app.add_typer(import_app, name="import")
app.add_typer(config_app, name="config")

console = Console()

MILESTONE_FOR_COMMAND = {
    "search": "M3 (WeKnora integration)",
    "ask": "M4 (retrieval + LLM)",
    "reindex": "M3 (WeKnora integration)",
}


def _load(ctx: typer.Context) -> Settings:
    """Resolve settings for a command, honouring the global ``--config``."""
    explicit = (ctx.obj or {}).get("config")
    settings = load_settings(explicit)
    configure_logging(
        settings.app.log_level,
        json_output=settings.app.log_json,
    )
    return settings


def _abort(exc: KnowMemoError) -> None:
    console.print(f"[red]✗[/red] {exc.code}: {exc}")
    raise typer.Exit(code=1)


def _todo(command: str, detail: str) -> None:
    """Report a command that is deliberately not implemented yet."""
    milestone = MILESTONE_FOR_COMMAND.get(command, "a later milestone")
    console.print(
        Panel(
            f"[yellow]{detail}[/yellow]\n\n"
            f"Scheduled for [bold]{milestone}[/bold].\n"
            "Nothing was read, written or uploaded.",
            title=f"{command} — not implemented yet",
            border_style="yellow",
        )
    )
    raise typer.Exit(code=2)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to a YAML configuration file.",
        exists=False,
    ),
    version: bool = typer.Option(False, "--version", help="Show the version and exit."),
) -> None:
    if version:
        console.print(f"KnowMemo {__version__}")
        raise typer.Exit()
    ctx.obj = {"config": config}


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check the local environment and report what is missing."""
    try:
        settings = _load(ctx)
    except ConfigError as exc:
        _abort(exc)
        return

    report = run_doctor(settings)

    table = Table(title="KnowMemo environment", show_lines=False, header_style="bold")
    table.add_column("", width=2, justify="center")
    table.add_column("Check", style="bold", no_wrap=True)
    table.add_column("Result")
    styles = {
        CheckStatus.OK: "green",
        CheckStatus.WARN: "yellow",
        CheckStatus.FAIL: "red",
        CheckStatus.SKIP: "dim",
    }
    for outcome in report.outcomes:
        table.add_row(
            f"[{styles[outcome.status]}]{outcome.symbol}[/{styles[outcome.status]}]",
            outcome.name,
            f"[{styles[outcome.status]}]{outcome.detail}[/{styles[outcome.status]}]",
        )
    console.print(table)

    remedies = [o for o in report.outcomes if o.remedy]
    if remedies:
        console.print("\n[bold]Suggested next steps[/bold]")
        for outcome in remedies:
            console.print(f"  [yellow]![/yellow] {outcome.name}: {outcome.remedy}")

    console.print(
        f"\n{report.ok_count} ok · {len(report.warnings)} warning(s) · "
        f"{len(report.failures)} failure(s)"
    )
    if report.failures:
        raise typer.Exit(code=report.exit_code)


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Print the fully resolved configuration."""
    try:
        settings = _load(ctx)
    except ConfigError as exc:
        _abort(exc)
        return

    payload = settings.model_dump(mode="json")
    payload["_source_path"] = str(settings.source_path) if settings.source_path else None
    console.print_json(json.dumps(payload, ensure_ascii=False))


@source_app.command("list")
def source_list(ctx: typer.Context) -> None:
    """List configured data sources."""
    settings = _load(ctx)
    engine = create_db_engine(settings)
    init_db(engine)

    with session_scope(make_session_factory(engine)) as session:
        sources = SourceRepository(session).list_all()

    if not sources:
        console.print(
            "No sources configured.\nAdd one with [bold]knowmemo source add wechat[/bold]."
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Type")
    table.add_column("Name")
    table.add_column("Enabled")
    table.add_column("Path")
    for source in sources:
        table.add_row(
            source.id,
            str(source.source_type),
            source.display_name,
            "yes" if source.enabled else "no",
            source.path or "[dim]not configured[/dim]",
        )
    console.print(table)


@source_app.command("add")
def source_add(
    ctx: typer.Context,
    source_type: str = typer.Argument(..., help="Source type, e.g. wechat."),
    snapshot_path: Path | None = typer.Option(
        None,
        "--snapshot-path",
        help="Directory of plaintext SQLite files. Overrides the config file.",
    ),
    account_id: str | None = typer.Option(
        None, "--account-id", help="Your own account id, used to attribute your messages."
    ),
    display_name: str | None = typer.Option(None, "--name", help="Label for the source."),
    disabled: bool = typer.Option(False, "--disabled", help="Register it but leave it off."),
) -> None:
    """Register a data source.

    A registered source is remembered in the metadata database. Configuration
    in ``knowmemo.yaml`` still wins where both exist, so a value set here never
    silently overrides an explicit one.
    """
    settings = _load(ctx)
    if source_type != "wechat":
        _todo(
            "source add",
            f"No connector is implemented for source type {source_type!r}.",
        )
        return

    engine = create_db_engine(settings)
    init_db(engine)
    connector = build_connector(
        settings,
        source_type,
        overrides={"snapshot_path": snapshot_path, "account_id": account_id},
    )
    discovered = connector.discover()

    with session_scope(make_session_factory(engine)) as session:
        repository = SourceRepository(session)
        repository.upsert(
            discovered.model_copy(
                update={
                    "display_name": display_name or discovered.display_name,
                    "enabled": not disabled,
                }
            )
        )

    _print_source_status(discovered, title=f"Source {discovered.id} registered")


def _print_source_status(source, *, title: str) -> None:
    table = Table(title=title, show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    styles = {
        SourceStatus.AVAILABLE: "green",
        SourceStatus.NOT_CONFIGURED: "yellow",
        SourceStatus.NOT_FOUND: "red",
        SourceStatus.UNREADABLE: "red",
        SourceStatus.UNSUPPORTED: "red",
    }
    style = styles.get(source.status, "yellow")
    table.add_row("Type", str(source.source_type))
    table.add_row("Path", source.path or "[dim]not configured[/dim]")
    table.add_row("Status", f"[{style}]{source.status}[/{style}]")
    if source.status_detail:
        table.add_row("Detail", source.status_detail)
    console.print(table)
    if source.status is not SourceStatus.AVAILABLE:
        raise typer.Exit(code=1)


@source_app.command("scan")
def source_scan(
    ctx: typer.Context,
    source_type: str = typer.Argument(..., help="Source type, e.g. wechat."),
) -> None:
    """Scan a source and report what is present, without importing anything."""
    settings = _load(ctx)
    engine = create_db_engine(settings)
    init_db(engine)

    with session_scope(make_session_factory(engine)) as session:
        overrides = _registered_overrides(SourceRepository(session), settings, source_type)

    connector = build_connector(settings, source_type, overrides=overrides)
    discovered = connector.discover()
    _print_source_status(discovered, title=f"Scan — {source_type}")

    result = connector.scan()

    table = Table(title="Contents", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Conversations", f"{result.conversations:,}")
    table.add_row("Messages", f"{result.messages:,}")
    table.add_row(
        "Earliest",
        result.earliest.strftime("%Y-%m-%d %H:%M") if result.earliest else "—",
    )
    table.add_row("Latest", result.latest.strftime("%Y-%m-%d %H:%M") if result.latest else "—")
    for key, value in result.details.items():
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value) or "—"
        elif isinstance(value, dict):
            rendered = ", ".join(f"{k}: {v}" for k, v in value.items()) or "—"
        else:
            rendered = str(value)
        table.add_row(key.replace("_", " ").capitalize(), rendered)
    console.print(table)

    unsupported = result.details.get("unsupported_types") or {}
    if unsupported:
        console.print(
            "\n[yellow]![/yellow] Message types this connector does not map yet "
            f"(they import as `unknown`): {unsupported}"
        )


def _registered_overrides(
    repository: SourceRepository, settings: Settings, source_type: str
) -> dict[str, object]:
    """Read a previously registered source, if configuration has no path.

    Precedence: an explicit setting in the configuration file wins; a source
    registered with ``source add`` fills the gap. Two stores of one value is a
    compromise, so the rule is written down rather than left to whichever code
    path runs first.
    """
    configured = settings.wechat.snapshot_path if source_type == "wechat" else None
    registered = next((s for s in repository.list_all() if str(s.source_type) == source_type), None)
    overrides: dict[str, object] = {}
    if configured is None and registered is not None and registered.path:
        overrides["snapshot_path"] = registered.path
    if source_type == "wechat" and settings.wechat.account_id is None and registered:
        account = registered.metadata.get("account_id")
        if account:
            overrides["account_id"] = account
    return overrides


@import_app.command("wechat")
def import_wechat(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be imported, writing nothing.",
    ),
    do_import: bool = typer.Option(
        False,
        "--import",
        help="Write normalised records and knowledge documents locally.",
    ),
    preview: int = typer.Option(5, "--preview", help="How many documents to preview."),
) -> None:
    """Run WeChat ingestion: normalise, segment, and build knowledge documents.

    Neither mode uploads anything. Sending documents to WeKnora is M3.
    """
    settings = _load(ctx)
    if dry_run and do_import:
        console.print("[red]✗[/red] --dry-run and --import are mutually exclusive.")
        raise typer.Exit(code=1)

    engine = create_db_engine(settings)
    init_db(engine)
    with session_scope(make_session_factory(engine)) as session:
        overrides = _registered_overrides(SourceRepository(session), settings, "wechat")

    try:
        pipeline = IngestionPipeline(
            settings,
            source_type="wechat",
            connector=build_connector(settings, "wechat", overrides=overrides),
        )
        report = pipeline.run(dry_run=dry_run, preview_limit=preview)
    except KnowMemoError as exc:
        _abort(exc)
        return

    _print_ingestion_report(report)
    if report.errors:
        raise typer.Exit(code=1)


def _print_ingestion_report(report: IngestionReport) -> None:
    heading = "Dry run — nothing was written" if report.dry_run else "Import complete"
    colour = {"success": "green", "partial": "yellow", "failed": "red"}.get(report.status, "yellow")
    console.print(f"[bold]{heading}[/bold] [{colour}]{report.status}[/{colour}]\n")

    table = Table(show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value", justify="right")
    for label, value in (
        ("Run", report.run_id),
        ("Source", report.source_id),
        ("Period", _format_period(report)),
        ("Conversations", f"{report.conversations:,}"),
        ("Messages scanned", f"{report.messages_scanned:,}"),
        ("Messages with text", f"{report.messages_with_text:,}"),
        ("Media-only messages", f"{report.messages_empty:,}"),
        ("Unknown message types", f"{report.unknown_message_types:,}"),
        ("Segments", f"{report.segments:,}"),
        (
            "Documents to create" if report.dry_run else "Documents created",
            f"{report.documents_created:,}",
        ),
        (
            "Documents to update" if report.dry_run else "Documents updated",
            f"{report.documents_updated:,}",
        ),
        ("Documents unchanged", f"{report.documents_unchanged:,}"),
        ("Errors", f"{report.errors:,}"),
    ):
        table.add_row(label, str(value))
    console.print(table)

    if report.preview:
        preview = Table(title="Documents", header_style="bold")
        preview.add_column("Action")
        preview.add_column("Conversation")
        preview.add_column("Period")
        preview.add_column("Msgs", justify="right")
        preview.add_column("~Tok", justify="right")
        preview.add_column("Participants")
        for item in report.preview:
            preview.add_row(
                item.action,
                item.conversation_name,
                f"{item.start_time.strftime('%Y-%m-%d %H:%M')} → {item.end_time.strftime('%H:%M')}",
                str(item.message_count),
                f"{item.estimated_tokens:,}",
                ", ".join(item.participants) or "—",
            )
        console.print()
        console.print(preview)
        if report.segments > len(report.preview):
            console.print(
                f"[dim]… {report.segments - len(report.preview):,} more not shown "
                "(--preview N to see more)[/dim]"
            )

    if report.failures:
        console.print("\n[bold]Failures[/bold]")
        for failure in report.failures[:10]:
            location = f" [{failure.record_id}]" if failure.record_id else ""
            console.print(f"  [red]✗[/red] {failure.stage}: {failure.code}{location}")
        if len(report.failures) > 10:
            console.print(f"  [dim]… {len(report.failures) - 10:,} more[/dim]")

    if report.warnings:
        console.print("\n[bold]Warnings[/bold]")
        for warning in report.warnings:
            console.print(f"  [yellow]![/yellow] {warning}")

    if report.dry_run:
        console.print(
            "\n[dim]Nothing was written. Run without --dry-run to materialise "
            "these documents.[/dim]"
        )
    else:
        console.print(f"\n[dim]Normalised records: {report.normalized_dir}[/dim]")
        console.print(f"[dim]Knowledge documents: {report.knowledge_dir}[/dim]")
        console.print("[dim]Nothing was uploaded. Sending documents to WeKnora is M3.[/dim]")


def _format_period(report: IngestionReport) -> str:
    if report.earliest is None or report.latest is None:
        return "—"
    return f"{report.earliest.strftime('%Y-%m-%d')} → {report.latest.strftime('%Y-%m-%d')}"


@import_app.command("status")
def import_status(ctx: typer.Context) -> None:
    """Show ingestion state."""
    settings = _load(ctx)
    engine = create_db_engine(settings)
    init_db(engine)

    with session_scope(make_session_factory(engine)) as session:
        latest = ImportRunRepository(session).latest()
        counts = RecordStateRepository(session).count_by_state()

    if latest is None:
        console.print("No import has been run yet.")
    else:
        table = Table(title=f"Latest import run {latest.id}", show_header=False)
        table.add_column("Field", style="bold")
        table.add_column("Value")
        for label, value in (
            ("Source", f"{latest.source_type}:{latest.source_id}"),
            ("Started", latest.started_at.isoformat()),
            ("Finished", latest.finished_at.isoformat() if latest.finished_at else "—"),
            ("Status", latest.status),
            ("Mode", "DRY RUN" if latest.dry_run else "IMPORT"),
            ("Messages scanned", f"{latest.messages_scanned:,}"),
            ("Messages imported", f"{latest.messages_imported:,}"),
            ("Documents created", f"{latest.documents_created:,}"),
            ("Documents updated", f"{latest.documents_updated:,}"),
            ("Errors", f"{latest.errors:,}"),
        ):
            table.add_row(label, str(value))
        console.print(table)

    if counts:
        console.print("\n[bold]Record states[/bold]")
        for state, count in sorted(counts.items()):
            console.print(f"  {state:<16} {count:,}")


@app.command()
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="What to search for."),
) -> None:
    """Search imported conversations."""
    _load(ctx)
    _todo("search", "Searching requires the WeKnora client.")


@app.command()
def ask(
    ctx: typer.Context,
    question: str = typer.Argument(..., help="Natural-language question."),
) -> None:
    """Ask a question about your history."""
    _load(ctx)
    _todo("ask", "Answering requires retrieval plus the LLM stage.")


@app.command()
def reindex(
    ctx: typer.Context,
    source_type: str = typer.Argument(..., help="Source type, e.g. wechat."),
) -> None:
    """Rebuild knowledge documents and indexes for a source."""
    _load(ctx)
    _todo("reindex", f"Re-indexing {source_type!r} requires the ingestion pipeline.")


@app.command()
def stats(ctx: typer.Context) -> None:
    """Show counts across the local store."""
    settings = _load(ctx)
    engine = create_db_engine(settings)
    init_db(engine)
    root = data_root(settings)

    with session_scope(make_session_factory(engine)) as session:
        sources = SourceRepository(session).list_all()
        segments = SegmentRepository(session).count()
        counts = RecordStateRepository(session).count_by_state()

    normalized_files = sorted((root / "normalized").glob("*.jsonl"))
    knowledge_files = sorted((root / "knowledge").glob("*.md"))
    normalized_records = sum(
        1
        for path in normalized_files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )

    table = Table(title=f"KnowMemo stats — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    for label, value in (
        ("Sources", len(sources)),
        ("Normalized records", normalized_records),
        ("Segments", segments),
        ("Knowledge documents", len(knowledge_files)),
        ("Indexed in WeKnora", counts.get("INDEXED", 0)),
        ("Failed records", sum(v for k, v in counts.items() if k.endswith("_FAILED"))),
    ):
        table.add_row(label, f"{value:,}")
    console.print(table)
    console.print(f"\n[dim]Data root: {root}[/dim]")


if __name__ == "__main__":  # pragma: no cover
    app()
