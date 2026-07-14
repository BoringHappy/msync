"""Command-line interface for msync."""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from msync.database import Archive
from msync.models import Provider
from msync.readers import HistoryFormatError, detect_provider, discover_transcripts

DEFAULT_DATABASE = Path.home() / ".msync" / "msync.sqlite"

app = typer.Typer(
    name="msync",
    help="Archive local Claude Code and Codex chat histories.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)


class ProviderChoice(StrEnum):
    """History formats accepted by the CLI."""

    auto = "auto"
    claude = "claude"
    codex = "codex"


@app.callback()
def cli() -> None:
    """Archive local Claude Code and Codex chat histories."""


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
            help="Claude or Codex data directory to archive.",
        ),
    ],
    database: Annotated[
        Path,
        typer.Option(
            "--database",
            "--db",
            help="SQLite archive path.",
            show_default=str(DEFAULT_DATABASE),
        ),
    ] = DEFAULT_DATABASE,
    provider: Annotated[
        ProviderChoice,
        typer.Option(help="History format; auto detects from the directory."),
    ] = ProviderChoice.auto,
) -> None:
    """Read new and changed transcripts into the SQLite archive."""

    root = directory.expanduser().resolve()
    database = database.expanduser().resolve()
    try:
        selected_provider: Provider = (
            detect_provider(root) if provider is ProviderChoice.auto else provider.value
        )
        transcripts = discover_transcripts(root, selected_provider)
        if not transcripts:
            raise HistoryFormatError(f"No conversation transcripts found in {root}.")
        result = Archive(database).upload(
            root=root,
            provider=selected_provider,
            transcripts=transcripts,
        )
    except (HistoryFormatError, OSError, RuntimeError, sqlite3.Error) as error:
        error_console.print(f"[bold red]Upload failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    table = Table(title="Upload complete", show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Provider", selected_provider)
    table.add_row("Location", str(root))
    table.add_row("Database", str(database))
    table.add_row("Transcripts", str(result.scanned))
    table.add_row("Imported", str(result.imported))
    table.add_row("Updated", str(result.updated))
    table.add_row("Unchanged", str(result.unchanged))
    table.add_row("Events indexed", str(result.events))
    console.print(table)


def main() -> None:
    """Run the Typer application."""

    app(prog_name="msync")
