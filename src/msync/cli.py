"""Command-line interface for msync."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from sqlalchemy.exc import SQLAlchemyError

from msync.database import Archive, SearchResult, UploadResult
from msync.providers import (
    HistoryFormatError,
    HistoryProvider,
    detect_provider,
    get_provider,
    provider_names,
)
from msync.synchronization import SyncResult, sync_conversations, unmanaged_transcripts

DEFAULT_DATABASE = Path.home() / ".msync" / "msync.sqlite"

app = typer.Typer(
    name="msync",
    help="Archive and synchronize local AI chat histories.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)


@app.callback()
def cli() -> None:
    """Archive and synchronize local AI chat histories."""


@app.command()
def upload(
    directory: Annotated[
        Path,
        typer.Option(
            "--dir",
            "-d",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Provider data directory to archive.",
        ),
    ],
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "--db",
            help="SQLite path or SQLAlchemy database URL.",
            show_default=str(DEFAULT_DATABASE),
        ),
    ] = str(DEFAULT_DATABASE),
    provider: Annotated[
        str,
        typer.Option(help=f"Provider name or auto detection ({', '.join(provider_names())})."),
    ] = "auto",
) -> None:
    """Read new and changed transcripts into the configured archive."""

    root = directory.expanduser().resolve()
    try:
        selected_provider = detect_provider(root) if provider == "auto" else get_provider(provider)
        transcripts = selected_provider.discover(root)
        if not transcripts:
            raise HistoryFormatError(f"No conversation transcripts found in {root}.")
        with Archive(database) as archive:
            result = archive.upload(
                root=root,
                provider=selected_provider,
                transcripts=transcripts,
            )
            database_display = archive.display_database
    except (HistoryFormatError, ImportError, OSError, RuntimeError, SQLAlchemyError) as error:
        error_console.print(f"[bold red]Upload failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    table = Table(title="Upload complete", show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Provider", selected_provider.name)
    table.add_row("Location", str(root))
    table.add_row("Database", database_display)
    table.add_row("Transcripts", str(result.scanned))
    table.add_row("Imported", str(result.imported))
    table.add_row("Updated", str(result.updated))
    table.add_row("Unchanged", str(result.unchanged))
    table.add_row("Duplicates skipped", str(result.duplicates))
    table.add_row("Events indexed", str(result.events))
    console.print(table)


@app.command()
def sync(
    directory: Annotated[
        list[Path],
        typer.Option(
            "--dir",
            "-d",
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Provider history directory to merge (repeat for each location).",
        ),
    ],
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "--db",
            help="SQLite path or SQLAlchemy database URL.",
            show_default=str(DEFAULT_DATABASE),
        ),
    ] = str(DEFAULT_DATABASE),
    provider: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Provider for each --dir, in the same order; omit for auto detection "
                f"({', '.join(provider_names())})."
            ),
        ),
    ] = None,
) -> None:
    """Merge histories through the archive and write each provider's native format."""

    roots = [path.expanduser().resolve() for path in directory]
    try:
        if len(set(roots)) != len(roots):
            raise HistoryFormatError("Each sync directory must be unique.")
        for root in roots:
            if root.exists() and not root.is_dir():
                raise HistoryFormatError(f"Sync location is not a directory: {root}")
        selected_providers = _sync_providers(roots, provider or [])
        for root in roots:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)

        with Archive(database) as archive:
            uploads = []
            for root, selected_provider in zip(roots, selected_providers, strict=True):
                transcripts = unmanaged_transcripts(
                    root,
                    selected_provider.discover(root),
                )
                uploads.append(
                    archive.upload(
                        root=root,
                        provider=selected_provider,
                        transcripts=transcripts,
                    )
                )
            conversations = archive.conversations()
            sync_results = [
                sync_conversations(
                    conversations,
                    destination=root,
                    provider=selected_provider,
                )
                for root, selected_provider in zip(roots, selected_providers, strict=True)
            ]
            database_display = archive.display_database
    except (HistoryFormatError, ImportError, OSError, RuntimeError, SQLAlchemyError) as error:
        error_console.print(f"[bold red]Sync failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    for root, selected_provider, upload_result, sync_result in zip(
        roots, selected_providers, uploads, sync_results, strict=True
    ):
        _print_sync_result(
            root=root,
            provider=selected_provider,
            database=database_display,
            upload=upload_result,
            result=sync_result,
        )


@app.command()
def server(
    password: Annotated[
        str,
        typer.Option(
            "--password",
            envvar="MSYNC_SERVER_PASSWORD",
            prompt="Server password",
            hide_input=True,
            help="Password required by the web UI (or set MSYNC_SERVER_PASSWORD).",
        ),
    ],
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "--db",
            help="SQLite path or SQLAlchemy database URL.",
            show_default=str(DEFAULT_DATABASE),
        ),
    ] = str(DEFAULT_DATABASE),
    host: Annotated[
        str,
        typer.Option(help="Address on which the web server listens."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(min=1, max=65535, help="TCP port on which the web server listens."),
    ] = 8000,
    username: Annotated[
        str,
        typer.Option(
            envvar="MSYNC_SERVER_USERNAME",
            help="Username required by the web UI.",
        ),
    ] = "msync",
) -> None:
    """Start the authenticated web UI for browsing archived chat history."""

    try:
        import uvicorn

        from msync.server import create_app

        web_app = create_app(database, username=username, password=password)
    except (ImportError, OSError, RuntimeError, SQLAlchemyError, ValueError) as error:
        error_console.print(f"[bold red]Server failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    console.print(f"History browser: [bold cyan]http://{display_host}:{port}[/bold cyan]")
    console.print(f"Sign in as [bold]{username}[/bold]. Press Ctrl+C to stop.")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        error_console.print(
            "[yellow]Security note:[/yellow] Basic authentication requires an HTTPS reverse "
            "proxy when exposed beyond this machine."
        )
    uvicorn.run(web_app, host=host, port=port)


@app.command()
def search(
    search_text: Annotated[
        str,
        typer.Argument(help="Text to find in archived conversation messages."),
    ],
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "--db",
            help="SQLite path or SQLAlchemy database URL.",
            show_default=str(DEFAULT_DATABASE),
        ),
    ] = str(DEFAULT_DATABASE),
) -> None:
    """Search archived conversation messages."""

    query = search_text.strip()
    if not query:
        error_console.print("[bold red]Search failed:[/bold red] Search text must not be empty.")
        raise typer.Exit(code=1)

    try:
        with Archive(database) as archive:
            results = archive.search(query)
    except (ImportError, OSError, RuntimeError, SQLAlchemyError) as error:
        error_console.print(f"[bold red]Search failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    if not results:
        console.print("No matches found for ", Text(query), ".", sep="")
        return

    _print_results("Search results", results)


@app.command()
def sample(
    limit: Annotated[
        int,
        typer.Argument(min=1, help="Maximum number of archived messages to inspect."),
    ],
    database: Annotated[
        str,
        typer.Option(
            "--database",
            "--db",
            help="SQLite path or SQLAlchemy database URL.",
            show_default=str(DEFAULT_DATABASE),
        ),
    ] = str(DEFAULT_DATABASE),
) -> None:
    """Show a random sample of archived conversation messages."""

    try:
        with Archive(database) as archive:
            results = archive.sample(limit)
    except (ImportError, OSError, RuntimeError, SQLAlchemyError, ValueError) as error:
        error_console.print(f"[bold red]Sample failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    if not results:
        console.print("No archived messages found.")
        return

    _print_results("Samples", results)


def _print_results(title: str, results: Sequence[SearchResult]) -> None:
    """Render message results with their archive context."""

    console.print(Text.assemble((title, "bold cyan"), (f" ({len(results)})", "bold")))
    for index, result in enumerate(results, start=1):
        heading = Text.assemble(
            (f" {index} ", "bold white on cyan"),
            (f" {result.provider} ", "bold cyan"),
        )
        console.print(Rule(heading, style="cyan"))

        metadata = Table.grid(padding=(0, 1))
        metadata.add_column(style="dim", no_wrap=True)
        metadata.add_column()
        metadata.add_row("Conversation", result.title or result.conversation_id)
        metadata.add_row("Time", result.occurred_at or "unknown")
        role = result.role or "unknown"
        role_style = {
            "assistant": "bold magenta",
            "system": "bold blue",
            "tool": "bold yellow",
            "user": "bold green",
        }.get(role, "bold")
        metadata.add_row("Role", Text(role, style=role_style))
        console.print(metadata)
        console.print()
        console.print(Text(result.text.strip()))


def _sync_providers(roots: list[Path], names: list[str]) -> list[HistoryProvider]:
    if names and len(names) != len(roots):
        raise HistoryFormatError("Pass exactly one --provider for each --dir, in the same order.")
    selections = names or ["auto"] * len(roots)
    return [
        detect_provider(root) if name == "auto" else get_provider(name)
        for root, name in zip(roots, selections, strict=True)
    ]


def _print_sync_result(
    *,
    root: Path,
    provider: HistoryProvider,
    database: str,
    upload: UploadResult,
    result: SyncResult,
) -> None:
    table = Table(title="Sync complete", show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Provider", provider.name)
    table.add_row("Location", str(root))
    table.add_row("Database", database)
    table.add_row("Transcripts scanned", str(upload.scanned))
    table.add_row("Archived", str(upload.imported))
    table.add_row("Archive updated", str(upload.updated))
    table.add_row("Archive unchanged", str(upload.unchanged))
    table.add_row("Archive duplicates skipped", str(upload.duplicates))
    table.add_row("Native histories kept", str(result.current))
    table.add_row("Native histories written", str(result.written))
    table.add_row("Native histories unchanged", str(result.unchanged))
    table.add_row("Existing histories protected", str(result.protected))
    table.add_row("Histories without messages skipped", str(result.skipped))
    table.add_row("Equivalent histories collapsed", str(result.equivalent))
    table.add_row("Path conflicts", str(len(result.conflicts)))
    console.print(table)
    for conflict in result.conflicts:
        error_console.print(f"[yellow]Not overwritten:[/yellow] {root / conflict}")


def main() -> None:
    """Run the Typer application."""

    app(prog_name="msync")
