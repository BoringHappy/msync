"""Command-line interface for msync."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from sqlalchemy.exc import SQLAlchemyError

from msync.database import (
    Archive,
    ArchivedConversation,
    SchemaUpgradeRequiredError,
    SearchResult,
    UploadResult,
)
from msync.hooks import queue_session_upload, wait_for_transcript_stable
from msync.providers import (
    HistoryFormatError,
    HistoryProvider,
    detect_provider,
    get_provider,
    provider_names,
)
from msync.remote import (
    UPLOAD_CONTENT_TYPE,
    UPLOAD_STREAM_CHUNK_BYTES,
    UPLOAD_TRANSCRIPT_MAX_BYTES,
    RemoteUploadMetadata,
    encode_upload_prefix,
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


@dataclass(slots=True, frozen=True)
class SessionVerificationFailure:
    """One transcript rejected before remote upload."""

    relative_path: str
    reason: str


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
    url: Annotated[
        str,
        typer.Option(
            envvar="MSYNC_ENDPOINT",
            help=(
                "Base URL of an msync server that accepts remote uploads (or set MSYNC_ENDPOINT)."
            ),
        ),
    ],
    transcript: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Upload only this transcript instead of scanning --dir.",
        ),
    ] = None,
    wait_for_transcript: Annotated[
        bool,
        typer.Option(
            "--wait-for-transcript",
            hidden=True,
            help="Wait for a selected transcript to stop changing before upload.",
        ),
    ] = False,
    expected_assistant_sha256: Annotated[
        str | None,
        typer.Option(
            "--expected-assistant-sha256",
            hidden=True,
            help="Wait until this assistant-message digest appears in the transcript.",
        ),
    ] = None,
    provider: Annotated[
        str,
        typer.Option(help=f"Provider name or auto detection ({', '.join(provider_names())})."),
    ] = "auto",
    hostname: Annotated[
        str | None,
        typer.Option(
            envvar="MSYNC_HOSTNAME",
            help="Hostname recorded for this source location (defaults to this machine).",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            envvar="MSYNC_TOKEN",
            help="API access token for --url (or set MSYNC_TOKEN).",
        ),
    ] = None,
) -> None:
    """Upload new and changed transcripts to a remote archive."""

    root = directory.expanduser().resolve()
    try:
        if not token:
            raise ValueError("--url requires --token or MSYNC_TOKEN.")
        selected_provider = detect_provider(root) if provider == "auto" else get_provider(provider)
        transcripts = _upload_transcripts(root, selected_provider, transcript)
        if not transcripts:
            raise HistoryFormatError(f"No conversation transcripts found in {root}.")
        if expected_assistant_sha256 is not None and not wait_for_transcript:
            raise ValueError("--expected-assistant-sha256 requires --wait-for-transcript.")
        if wait_for_transcript:
            if transcript is None:
                raise ValueError("--wait-for-transcript requires --transcript.")
            wait_for_transcript_stable(
                transcripts[0],
                provider=selected_provider,
                root=root,
                expected_assistant_sha256=expected_assistant_sha256,
            )
        verified_transcripts, verification_failures = _verify_transcripts(
            root=root,
            provider=selected_provider,
            transcripts=transcripts,
        )
        _print_session_verification(
            verified=len(verified_transcripts),
            failures=verification_failures,
        )
        if not verified_transcripts:
            raise HistoryFormatError("No sessions passed the pre-upload verification.")
        location_hostname = _upload_hostname(hostname)
        result, target_display = _remote_upload(
            url=url,
            token=token,
            root=root,
            hostname=location_hostname,
            provider=selected_provider,
            transcripts=verified_transcripts,
        )
    except (
        HistoryFormatError,
        ImportError,
        OSError,
        RuntimeError,
        SQLAlchemyError,
        ValueError,
    ) as error:
        error_console.print(f"[bold red]Upload failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    table = Table(
        title="Upload incomplete" if verification_failures else "Upload complete",
        show_header=False,
        box=None,
        pad_edge=False,
    )
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Provider", selected_provider.name)
    table.add_row("Hostname", location_hostname)
    table.add_row("Location", str(root))
    table.add_row("Server", target_display)
    table.add_row("Transcripts", str(len(transcripts)))
    table.add_row("Sessions verified", str(len(verified_transcripts)))
    table.add_row("Sessions failed", str(len(verification_failures)))
    table.add_row("Imported", str(result.imported))
    table.add_row("Updated", str(result.updated))
    table.add_row("Unchanged", str(result.unchanged))
    table.add_row("Duplicates skipped", str(result.duplicates))
    table.add_row("Events indexed", str(result.events))
    console.print(table)
    if verification_failures:
        raise typer.Exit(code=1)


@app.command("upload-hook", hidden=True)
def upload_hook(
    provider: Annotated[
        str | None,
        typer.Option(help=f"Provider override ({', '.join(provider_names())})."),
    ] = None,
) -> None:
    """Queue one transcript upload from a Claude Code or Codex Stop hook."""

    try:
        queue_session_upload(provider)
    except (OSError, RuntimeError, ValueError) as error:
        error_console.print(f"[bold red]Upload hook failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error


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
    url: Annotated[
        str,
        typer.Option(
            envvar="MSYNC_ENDPOINT",
            help="Base URL of the msync server to synchronize (or set MSYNC_ENDPOINT).",
        ),
    ],
    provider: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Provider for each --dir, in the same order; omit for auto detection "
                f"({', '.join(provider_names())})."
            ),
        ),
    ] = None,
    hostname: Annotated[
        str | None,
        typer.Option(
            envvar="MSYNC_HOSTNAME",
            help="Hostname recorded for these source locations (defaults to this machine).",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            envvar="MSYNC_TOKEN",
            help="API access token for --url (or set MSYNC_TOKEN).",
        ),
    ] = None,
) -> None:
    """Merge remote archive histories into each provider's native format."""

    roots = [path.expanduser().resolve() for path in directory]
    try:
        if not token:
            raise ValueError("--url requires --token or MSYNC_TOKEN.")
        if len(set(roots)) != len(roots):
            raise HistoryFormatError("Each sync directory must be unique.")
        for root in roots:
            if root.exists() and not root.is_dir():
                raise HistoryFormatError(f"Sync location is not a directory: {root}")
        selected_providers = _sync_providers(roots, provider or [])
        for root in roots:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)

        verified_by_root: list[list[Path]] = []
        verification_failures: list[SessionVerificationFailure] = []
        for root, selected_provider in zip(roots, selected_providers, strict=True):
            transcripts = unmanaged_transcripts(root, selected_provider.discover(root))
            verified, failures = _verify_transcripts(
                root=root,
                provider=selected_provider,
                transcripts=transcripts,
            )
            verified_by_root.append(verified)
            verification_failures.extend(failures)
        if verification_failures:
            _print_session_verification(
                verified=sum(len(transcripts) for transcripts in verified_by_root),
                failures=tuple(verification_failures),
            )
            raise HistoryFormatError(
                "Sync stopped before upload because a session failed validation."
            )

        location_hostname = _upload_hostname(hostname)
        uploads: list[UploadResult] = []
        server_display = _server_url(url)
        for root, selected_provider, transcripts in zip(
            roots,
            selected_providers,
            verified_by_root,
            strict=True,
        ):
            if transcripts:
                upload_result, server_display = _remote_upload(
                    url=server_display,
                    token=token,
                    root=root,
                    hostname=location_hostname,
                    provider=selected_provider,
                    transcripts=transcripts,
                )
            else:
                upload_result = UploadResult(location_id=0)
            uploads.append(upload_result)

        conversations = _remote_conversations(server_display, token)
        sync_results = [
            sync_conversations(
                conversations,
                destination=root,
                provider=selected_provider,
                current_hostname=location_hostname,
            )
            for root, selected_provider in zip(roots, selected_providers, strict=True)
        ]
    except (
        HistoryFormatError,
        ImportError,
        OSError,
        RuntimeError,
        SQLAlchemyError,
        ValueError,
    ) as error:
        error_console.print(f"[bold red]Sync failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    for root, selected_provider, upload_result, sync_result in zip(
        roots, selected_providers, uploads, sync_results, strict=True
    ):
        _print_sync_result(
            root=root,
            provider=selected_provider,
            hostname=location_hostname,
            server=server_display,
            upload=upload_result,
            result=sync_result,
        )


def _upgrade_server_database(database: str, *, lock_timeout: int = 10) -> None:
    """Upgrade an archive schema before starting the server."""

    steps: list[tuple[int, int]] = []

    def report_upgrade(current_version: int, target_version: int) -> None:
        steps.append((current_version, target_version))
        detail = {
            (5, 6): "rebuilding normalized events from retained transcripts",
        }.get((current_version, target_version), "applying archive changes")
        console.print(f"Upgrading database schema {current_version} → {target_version}: {detail}.")

    def report_progress(completed: int, total: int) -> None:
        if completed == 0 or completed == total or completed % 100 == 0:
            console.print(f"Reindexing conversations: {completed}/{total}")

    with Archive(
        database,
        schema_lock_timeout=lock_timeout,
        upgrade_reporter=report_upgrade,
        upgrade_progress_reporter=report_progress,
    ) as archive:
        initialized = archive.initialized_new_database

    if initialized:
        console.print(f"Database initialized at schema version {archive.schema_version}.")
    elif steps:
        console.print(
            f"Database schema upgrade complete: {steps[0][0]} → {archive.schema_version}."
        )
    else:
        console.print(f"Database schema is current at version {archive.schema_version}.")


@app.command()
def server(
    password: Annotated[
        str | None,
        typer.Option(
            "--password",
            envvar="MSYNC_SERVER_PASSWORD",
            hide_input=True,
            help="Single-user web password (or set MSYNC_SERVER_PASSWORD).",
        ),
    ] = None,
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
    accounts: Annotated[
        str | None,
        typer.Option(
            envvar="MSYNC_SERVER_ACCOUNTS",
            help=(
                "Semicolon-separated username,password[,token] accounts "
                "(or set MSYNC_SERVER_ACCOUNTS)."
            ),
        ),
    ] = None,
) -> None:
    """Start the authenticated history browser and remote-upload API."""

    database = os.environ.get("MSYNC_DATABASE_URL") or str(DEFAULT_DATABASE)
    console.print("Checking database schema...")
    try:
        import uvicorn

        from msync.server import create_app, parse_server_accounts

        configured_accounts = parse_server_accounts(accounts) if accounts is not None else None
        if configured_accounts is None:
            selected_password = password
            if selected_password is None:
                selected_password = typer.prompt("Server password", hide_input=True)

            def build_app() -> object:
                return create_app(database, username=username, password=selected_password)

            login_display = username
        else:

            def build_app() -> object:
                return create_app(database, accounts=configured_accounts)

            login_display = f"one of {len(configured_accounts)} configured accounts"

        try:
            web_app = build_app()
        except SchemaUpgradeRequiredError as error:
            console.print(
                f"Database schema version {error.current_version} must be upgraded to "
                f"{error.target_version} before the server can start."
            )
            if not typer.confirm("Upgrade the database now?", default=False):
                error_console.print("Server not started; the database was not upgraded.")
                raise typer.Exit(code=1) from error
            _upgrade_server_database(database)
            web_app = build_app()
    except typer.Exit:
        raise
    except (ImportError, OSError, RuntimeError, SQLAlchemyError, ValueError) as error:
        error_console.print(f"[bold red]Server failed:[/bold red] {error}")
        raise typer.Exit(code=1) from error

    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    console.print(f"History browser: [bold cyan]http://{display_host}:{port}[/bold cyan]")
    console.print(f"Sign in as [bold]{login_display}[/bold]. Press Ctrl+C to stop.")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        error_console.print(
            "[yellow]Security note:[/yellow] Basic and token authentication require an HTTPS "
            "reverse proxy when exposed beyond this machine."
        )
    uvicorn.run(web_app, host=host, port=port)


@app.command()
def search(
    search_text: Annotated[
        str,
        typer.Argument(help="Text to find in archived conversation messages."),
    ],
    url: Annotated[
        str,
        typer.Option(
            envvar="MSYNC_ENDPOINT",
            help="Base URL of the msync server to search (or set MSYNC_ENDPOINT).",
        ),
    ],
    token: Annotated[
        str | None,
        typer.Option(
            envvar="MSYNC_TOKEN",
            help="API access token for --url (or set MSYNC_TOKEN).",
        ),
    ] = None,
) -> None:
    """Search remote archived conversation messages."""

    query = search_text.strip()
    if not query:
        error_console.print("[bold red]Search failed:[/bold red] Search text must not be empty.")
        raise typer.Exit(code=1)

    try:
        if not token:
            raise ValueError("--url requires --token or MSYNC_TOKEN.")
        conversations = _remote_conversations(url, token, search_text=query)
        results = _conversation_results(conversations, search_text=query)
    except (ImportError, OSError, RuntimeError, SQLAlchemyError, ValueError) as error:
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
        typer.Argument(min=1, max=500, help="Maximum number of archived messages to inspect."),
    ],
    url: Annotated[
        str,
        typer.Option(
            envvar="MSYNC_ENDPOINT",
            help="Base URL of the msync server to sample (or set MSYNC_ENDPOINT).",
        ),
    ],
    token: Annotated[
        str | None,
        typer.Option(
            envvar="MSYNC_TOKEN",
            help="API access token for --url (or set MSYNC_TOKEN).",
        ),
    ] = None,
) -> None:
    """Show a random sample of remote archived conversation messages."""

    try:
        if not token:
            raise ValueError("--url requires --token or MSYNC_TOKEN.")
        results = _remote_sample(url, token, limit)
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


def _upload_hostname(hostname: str | None) -> str:
    value = socket.gethostname() if hostname is None else hostname
    normalized = value.strip()
    if not normalized:
        raise ValueError("Location hostname must not be empty.")
    if len(normalized) > 255:
        raise ValueError("Location hostname must not exceed 255 characters.")
    return normalized


def _upload_transcripts(
    root: Path,
    provider: HistoryProvider,
    transcript: Path | None,
) -> list[Path]:
    if transcript is None:
        return provider.discover(root)
    path = transcript.expanduser().resolve()
    if not path.is_relative_to(root):
        raise HistoryFormatError(f"Transcript must be contained in --dir: {path}")
    if path.suffix.casefold() != ".jsonl" or path.name in provider.ignored_filenames:
        raise HistoryFormatError(f"Not a {provider.name} conversation transcript: {path}")
    return [path]


def _verify_transcripts(
    *,
    root: Path,
    provider: HistoryProvider,
    transcripts: list[Path],
) -> tuple[list[Path], tuple[SessionVerificationFailure, ...]]:
    """Parse every session locally and collect failures before opening the network."""

    verified: list[Path] = []
    failures: list[SessionVerificationFailure] = []
    for path in transcripts:
        relative_path = path.relative_to(root).as_posix()
        try:
            if path.stat().st_size > UPLOAD_TRANSCRIPT_MAX_BYTES:
                raise ValueError("Transcript exceeds the 256 MiB remote upload limit")
            conversation = provider.read(path, root)
        except (OSError, ValueError) as error:
            failures.append(
                SessionVerificationFailure(
                    relative_path=relative_path,
                    reason=_single_line_error(error),
                )
            )
            continue

        invalid_events = [event for event in conversation.events if event.parse_error]
        if not conversation.events:
            reason = "transcript contains no JSON records"
        elif invalid_events:
            first_invalid = invalid_events[0]
            reason = (
                f"line {first_invalid.sequence + 1}: "
                f"{_single_line_error(first_invalid.parse_error or 'invalid record')}"
            )
            if len(invalid_events) > 1:
                reason += f" ({len(invalid_events) - 1} more invalid records)"
        elif conversation.started_at is None:
            reason = "session contains no event timestamps"
        else:
            verified.append(path)
            continue

        failures.append(SessionVerificationFailure(relative_path=relative_path, reason=reason))
    return verified, tuple(failures)


def _single_line_error(error: object) -> str:
    """Keep provider validation details readable in a one-line CLI report."""

    return " ".join(str(error).split())


def _print_session_verification(
    *,
    verified: int,
    failures: tuple[SessionVerificationFailure, ...],
) -> None:
    """Report the pre-upload result and a reason for every rejected session."""

    failed = len(failures)
    console.print(f"Session pre-check: {verified} passed, {failed} failed.")
    for failure in failures:
        error_console.print(
            Text.assemble(
                ("Session not uploaded: ", "bold red"),
                (failure.relative_path, "bold"),
            )
        )
        error_console.print(Text.assemble(("  Reason: ", "dim"), failure.reason))


def _server_url(url: str) -> str:
    """Validate and normalize an msync server base URL."""

    try:
        server_url = httpx.URL(url)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid msync server URL: {error}") from error
    if server_url.scheme not in {"http", "https"} or not server_url.host:
        raise ValueError("msync server URL must be an absolute http:// or https:// URL.")
    if server_url.username or server_url.password or server_url.query or server_url.fragment:
        raise ValueError(
            "msync server URL must not contain credentials, a query string, or a fragment."
        )
    return str(server_url).rstrip("/")


def _remote_json(
    url: str,
    token: str,
    path: str,
    *,
    params: dict[str, str | int] | None = None,
) -> object:
    """Read one authenticated JSON API response without following redirects."""

    server_url = _server_url(url)
    try:
        response = httpx.get(
            f"{server_url}{path}",
            params=params,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
        )
    except httpx.HTTPError as error:
        raise RuntimeError(f"Could not reach msync server {server_url}: {error}") from error
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except ValueError, AttributeError:
            detail = None
        message = detail if isinstance(detail, str) and detail else response.reason_phrase
        raise RuntimeError(f"msync server returned HTTP {response.status_code}: {message}")
    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError("msync server returned invalid JSON.") from error


def _remote_conversations(
    url: str,
    token: str,
    *,
    search_text: str = "",
) -> list[ArchivedConversation]:
    """Reconstruct the authenticated account's raw-event archive through the public API."""

    locations_payload = _remote_json(url, token, "/api/locations")
    if not isinstance(locations_payload, list):
        raise RuntimeError("msync server returned an invalid location list.")
    location_details: dict[int, tuple[str, str]] = {}
    for item in locations_payload:
        if not isinstance(item, dict):
            raise RuntimeError("msync server returned an invalid location.")
        location_id = item.get("id")
        hostname = item.get("hostname")
        root_path = item.get("root_path")
        if (
            not isinstance(location_id, int)
            or not isinstance(hostname, str)
            or not hostname
            or not isinstance(root_path, str)
            or not root_path
        ):
            raise RuntimeError("msync server returned an invalid location.")
        location_details[location_id] = (hostname, root_path)

    summaries: list[dict[str, object]] = []
    offset = 0
    while True:
        page = _remote_json(
            url,
            token,
            "/api/conversations",
            params={
                "search": search_text,
                "order": "oldest",
                "limit": 500,
                "offset": offset,
            },
        )
        if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
            raise RuntimeError("msync server returned an invalid conversation list.")
        summaries.extend(page)
        if len(page) < 500:
            break
        offset += len(page)

    conversations: list[ArchivedConversation] = []
    for summary in summaries:
        conversation_id = summary.get("id")
        location_id = summary.get("location_id")
        provider_name = summary.get("provider")
        if (
            not isinstance(conversation_id, int)
            or not isinstance(location_id, int)
            or not isinstance(provider_name, str)
            or location_id not in location_details
        ):
            raise RuntimeError("msync server returned an invalid conversation summary.")
        detail = _remote_json(url, token, f"/api/conversations/{conversation_id}")
        if not isinstance(detail, dict):
            raise RuntimeError("msync server returned an invalid conversation.")
        relative_path = detail.get("relative_path")
        events = detail.get("events")
        if not isinstance(relative_path, str) or not relative_path or not isinstance(events, list):
            raise RuntimeError("msync server returned an invalid conversation.")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("msync server returned an unsafe conversation path.")
        raw_records: list[str] = []
        for event in events:
            raw_json = event.get("raw_json") if isinstance(event, dict) else None
            if not isinstance(raw_json, str) or not raw_json:
                raise RuntimeError("msync server returned an invalid conversation event.")
            raw_records.append(raw_json)
        transcript = ("\n".join(raw_records) + "\n").encode()
        source_hostname, source_root = location_details[location_id]
        root = Path(source_root)
        provider = get_provider(provider_name)
        conversation = provider.read(root / relative, root, transcript=transcript)
        conversations.append(
            ArchivedConversation(
                location_id=location_id,
                source_hostname=source_hostname,
                source_root=source_root,
                source_mtime_ns=0,
                conversation=conversation,
            )
        )
    return conversations


def _remote_sample(url: str, token: str, limit: int) -> list[SearchResult]:
    """Request a bounded random message sample from the authenticated archive."""

    payload = _remote_json(url, token, "/api/sample", params={"limit": limit})
    if not isinstance(payload, list):
        raise RuntimeError("msync server returned an invalid message sample.")
    results: list[SearchResult] = []
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("msync server returned an invalid sampled message.")
        provider = item.get("provider")
        conversation_id = item.get("conversation_id")
        title = item.get("title")
        relative_path = item.get("relative_path")
        role = item.get("role")
        occurred_at = item.get("occurred_at")
        event_text = item.get("text")
        if (
            not isinstance(provider, str)
            or not isinstance(conversation_id, str)
            or (title is not None and not isinstance(title, str))
            or not isinstance(relative_path, str)
            or (role is not None and not isinstance(role, str))
            or (occurred_at is not None and not isinstance(occurred_at, str))
            or not isinstance(event_text, str)
        ):
            raise RuntimeError("msync server returned an invalid sampled message.")
        results.append(
            SearchResult(
                provider=provider,
                conversation_id=conversation_id,
                title=title,
                relative_path=relative_path,
                role=role,
                occurred_at=occurred_at,
                text=event_text,
            )
        )
    return results


def _conversation_results(
    conversations: list[ArchivedConversation],
    *,
    search_text: str = "",
) -> list[SearchResult]:
    """Flatten remote normalized events into the CLI's rich search result view."""

    query = search_text.casefold()
    results: list[SearchResult] = []
    for archived in conversations:
        conversation = archived.conversation
        for event in conversation.events:
            event_text = event.searchable_text.strip()
            if not event_text or (query and query not in event_text.casefold()):
                continue
            results.append(
                SearchResult(
                    provider=conversation.provider,
                    conversation_id=conversation.external_id,
                    title=conversation.title,
                    relative_path=conversation.relative_path,
                    role=event.role,
                    occurred_at=event.occurred_at,
                    text=event_text,
                )
            )
    return results


def _remote_upload(
    *,
    url: str,
    token: str,
    root: Path,
    hostname: str,
    provider: HistoryProvider,
    transcripts: list[Path],
) -> tuple[UploadResult, str]:
    server_url = _server_url(url)
    endpoint = f"{server_url}/api/upload"
    aggregate: UploadResult | None = None
    timeout = httpx.Timeout(connect=10, read=None, write=None, pool=10)
    for path in transcripts:
        stat = path.stat()
        if stat.st_size > UPLOAD_TRANSCRIPT_MAX_BYTES:
            raise ValueError(f"Transcript exceeds the 256 MiB remote upload limit: {path}")
        metadata = RemoteUploadMetadata(
            provider=provider.name,
            hostname=hostname,
            root_path=str(root),
            display_name=root.name or str(root),
            relative_path=path.relative_to(root).as_posix(),
            source_mtime_ns=stat.st_mtime_ns,
        )
        prefix = encode_upload_prefix(metadata)
        try:
            response = httpx.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": UPLOAD_CONTENT_TYPE,
                    "Content-Length": str(len(prefix) + stat.st_size),
                },
                content=_stream_remote_transcript(prefix, path),
                timeout=timeout,
            )
        except httpx.HTTPError as error:
            raise RuntimeError(f"Could not reach msync server {server_url}: {error}") from error
        file_result = _remote_upload_response(response)
        if aggregate is None:
            aggregate = UploadResult(location_id=file_result.location_id)
        elif aggregate.location_id != file_result.location_id:
            raise RuntimeError("msync server changed location IDs during the upload.")
        for field_name in (
            "scanned",
            "imported",
            "updated",
            "unchanged",
            "duplicates",
            "events",
            "message_parts",
        ):
            setattr(
                aggregate,
                field_name,
                getattr(aggregate, field_name) + getattr(file_result, field_name),
            )
    if aggregate is None:
        raise RuntimeError("Remote upload requires at least one transcript.")
    return aggregate, server_url


def _stream_remote_transcript(prefix: bytes, path: Path) -> Iterator[bytes]:
    yield prefix
    with path.open("rb") as stream:
        while chunk := stream.read(UPLOAD_STREAM_CHUNK_BYTES):
            yield chunk


def _remote_upload_response(response: httpx.Response) -> UploadResult:
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except ValueError, AttributeError:
            detail = None
        message = detail if isinstance(detail, str) and detail else response.reason_phrase
        raise RuntimeError(f"msync server returned HTTP {response.status_code}: {message}")
    try:
        values = response.json()
        return UploadResult(
            location_id=int(values["location_id"]),
            scanned=int(values["scanned"]),
            imported=int(values["imported"]),
            updated=int(values["updated"]),
            unchanged=int(values["unchanged"]),
            duplicates=int(values["duplicates"]),
            events=int(values["events"]),
            message_parts=int(values["message_parts"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("msync server returned an invalid upload response.") from error


def _print_sync_result(
    *,
    root: Path,
    provider: HistoryProvider,
    hostname: str,
    server: str,
    upload: UploadResult,
    result: SyncResult,
) -> None:
    table = Table(title="Sync complete", show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Provider", provider.name)
    table.add_row("Hostname", hostname)
    table.add_row("Location", str(root))
    table.add_row("Server", server)
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
