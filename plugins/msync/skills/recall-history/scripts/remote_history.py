#!/usr/bin/env python3
"""Search and read conversations from an authenticated remote msync archive."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_SEARCH_LIMIT = 10
DEFAULT_EVENT_LIMIT = 200
DEFAULT_EVENT_MAX_CHARS = 12_000
REQUEST_TIMEOUT_SECONDS = 20


class RecallError(RuntimeError):
    """A safe, user-facing remote recall failure."""


class _RejectRedirects(HTTPRedirectHandler):
    """Keep an Authorization header from following a redirect to another origin."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search and read conversations from a remote msync archive.",
    )
    parser.add_argument(
        "--url",
        help="msync server base URL (defaults to MSYNC_UPLOAD_URL).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="Search conversation summaries.")
    search.add_argument("query", nargs="?", default="", help="Text to find.")
    search.add_argument(
        "--project",
        action="store_true",
        help="Keep only conversations whose working directory matches this Git repository.",
    )
    search.add_argument("--limit", type=_bounded(1, 50), default=DEFAULT_SEARCH_LIMIT)
    search.add_argument("--offset", type=_bounded(0, 100_000), default=0)
    search.add_argument(
        "--order",
        choices=("newest", "oldest", "messages", "events", "title"),
        default="newest",
    )

    read = subparsers.add_parser("read", help="Read one conversation by archive ID.")
    read.add_argument("conversation_id", type=_bounded(1, 2**63 - 1))
    read.add_argument("--limit", type=_bounded(1, 500), default=DEFAULT_EVENT_LIMIT)
    read.add_argument("--offset", type=_bounded(0, 100_000_000), default=0)
    read.add_argument(
        "--max-chars",
        type=_bounded(0, 1_000_000),
        default=DEFAULT_EVENT_MAX_CHARS,
        help="Maximum characters per event; use 0 for no limit.",
    )
    read.add_argument(
        "--all-events",
        action="store_true",
        help="Include searchable tool and system activity, not only user/assistant messages.",
    )
    return parser


def _bounded(minimum: int, maximum: int) -> Any:
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("must be an integer") from error
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return number

    return parse


def _base_url(override: str | None) -> str:
    value = (override or os.environ.get("MSYNC_UPLOAD_URL", "")).strip()
    if not value:
        raise RecallError("Set MSYNC_UPLOAD_URL to the remote msync server URL.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RecallError("MSYNC_UPLOAD_URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RecallError("MSYNC_UPLOAD_URL must not contain credentials, a query, or a fragment.")
    return value.rstrip("/")


def _token() -> str:
    value = os.environ.get("MSYNC_TOKEN") or os.environ.get("MSYNC_UPLOAD_TOKEN")
    if not value:
        raise RecallError("Set MSYNC_TOKEN to an msync account access token.")
    return value


def _request_json(base_url: str, token: str, path: str, params: dict[str, Any]) -> Any:
    query = urlencode(params)
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{query}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "msync-recall-history/0.1",
        },
    )
    try:
        with build_opener(_RejectRedirects()).open(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            return json.load(response)
    except HTTPError as error:
        detail = ""
        try:
            payload = json.load(error)
            if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
                detail = f": {payload['detail']}"
        except UnicodeDecodeError, json.JSONDecodeError:
            pass
        raise RecallError(f"msync returned HTTP {error.code}{detail}") from error
    except URLError as error:
        raise RecallError(f"Could not reach the msync server: {error.reason}") from error


def _project_aliases() -> set[str]:
    root = _git_output("rev-parse", "--show-toplevel")
    current_root = Path(root) if root else Path.cwd()
    aliases = {current_root.name.casefold()}
    remote = _git_output("remote", "get-url", "origin")
    if remote:
        remote_name = re.split(r"[/\\:]", remote.removesuffix(".git"))[-1]
        if remote_name:
            aliases.add(remote_name.casefold())
    return aliases


def _git_output(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except OSError, subprocess.CalledProcessError:
        return None
    return result.stdout.strip() or None


def _matches_project(item: dict[str, Any], aliases: set[str]) -> bool:
    cwd = item.get("cwd")
    if not isinstance(cwd, str):
        return False
    segments = {segment.casefold() for segment in re.split(r"[/\\]+", cwd) if segment}
    return bool(segments & aliases)


def _search(args: argparse.Namespace, base_url: str, token: str) -> None:
    request_limit = 500 if args.project else args.limit
    request_offset = 0 if args.project else args.offset
    payload = _request_json(
        base_url,
        token,
        "/api/conversations",
        {
            "search": args.query.strip(),
            "order": args.order,
            "limit": request_limit,
            "offset": request_offset,
        },
    )
    if not isinstance(payload, list):
        raise RecallError("msync returned an invalid conversation list.")
    conversations = payload
    if args.project:
        aliases = _project_aliases()
        conversations = [
            item
            for item in conversations
            if isinstance(item, dict) and _matches_project(item, aliases)
        ]
        conversations = conversations[args.offset : args.offset + args.limit]

    if not conversations:
        print("No matching conversations found.")
        return
    print(f"Found {len(conversations)} conversation(s):")
    for item in conversations:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id", "?")
        provider = item.get("provider", "unknown")
        activity = item.get("ended_at") or item.get("started_at") or "unknown time"
        title = item.get("title") or item.get("external_id") or "untitled"
        print(f"\n{identifier} | {provider} | {activity} | {_one_line(title, 160)}")
        if cwd := item.get("cwd"):
            print(f"  cwd: {cwd}")
        if branch := item.get("git_branch"):
            print(f"  branch: {branch}")
        if preview := item.get("preview"):
            print(f"  preview: {_one_line(preview, 300)}")


def _read(args: argparse.Namespace, base_url: str, token: str) -> None:
    payload = _request_json(
        base_url,
        token,
        f"/api/conversations/{args.conversation_id}",
        {"event_limit": args.limit, "event_offset": args.offset},
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), dict):
        raise RecallError("msync returned an invalid conversation.")
    summary = payload["summary"]
    title = summary.get("title") or summary.get("external_id") or "untitled"
    print(f"Conversation {summary.get('id', args.conversation_id)}: {title}")
    print(f"Provider: {summary.get('provider', 'unknown')}")
    if cwd := summary.get("cwd"):
        print(f"Working directory: {cwd}")
    if branch := summary.get("git_branch"):
        print(f"Git branch: {branch}")

    printed = 0
    events = payload.get("events")
    if not isinstance(events, list):
        raise RecallError("msync returned invalid conversation events.")
    for event in events:
        if not isinstance(event, dict):
            continue
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        role = event.get("role")
        if not args.all_events and role not in {"user", "assistant"}:
            continue
        label = role or event.get("event_subtype") or event.get("event_type") or "event"
        occurred_at = event.get("occurred_at")
        suffix = f" | {occurred_at}" if occurred_at else ""
        print(f"\n[{event.get('sequence', '?')} | {label}{suffix}]")
        print(_bounded_text(text.strip(), args.max_chars))
        printed += 1
    if not printed:
        print("\nNo visible messages in this event page.")

    event_count = summary.get("event_count")
    next_offset = args.offset + args.limit
    if isinstance(event_count, int) and next_offset < event_count:
        print(f"\nMore events available; continue with --offset {next_offset}.")


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _bounded_text(text: str, limit: int) -> str:
    if limit == 0 or len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n[… {omitted} characters omitted; raise --max-chars to include them]"


def main() -> int:
    args = _parser().parse_args()
    try:
        base_url = _base_url(args.url)
        token = _token()
        if args.command == "search":
            _search(args, base_url, token)
        else:
            _read(args, base_url, token)
    except RecallError as error:
        print(f"Recall failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
