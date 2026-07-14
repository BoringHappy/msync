"""Command-line interface for msync."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy.exc import SQLAlchemyError

from msync.database import Archive
from msync.providers import (
    HistoryFormatError,
    detect_provider,
    get_provider,
    provider_names,
)

DEFAULT_DATABASE = Path.home() / ".msync" / "msync.sqlite"

app = typer.Typer(
    name="msync",
    help="Archive local AI chat histories.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)


@app.callback()
def cli() -> None:
    """Archive local AI chat histories."""


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
    table.add_row("Events indexed", str(result.events))
    console.print(table)


def main() -> None:
    """Run the Typer application."""

    app(prog_name="msync")
