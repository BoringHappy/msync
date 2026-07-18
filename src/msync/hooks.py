"""Non-blocking upload launcher for Claude Code and Codex lifecycle hooks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from msync.providers import get_provider

_PROVIDER_ROOT_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
}
_PROVIDER_TRANSCRIPT_DIRECTORIES = {
    "claude": frozenset({"projects"}),
    "codex": frozenset({"sessions", "archived_sessions"}),
}
_TRANSCRIPT_QUIET_SECONDS = 2.0
_TRANSCRIPT_WAIT_TIMEOUT_SECONDS = 10.0
_TRANSCRIPT_POLL_SECONDS = 0.1


def queue_session_upload(
    provider_name: str | None = None,
    *,
    input_stream: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Start a detached one-transcript upload and return without waiting for it."""

    environment = os.environ if environ is None else environ
    if not environment.get("MSYNC_UPLOAD_URL") or not environment.get("MSYNC_UPLOAD_TOKEN"):
        return False

    stream = sys.stdin if input_stream is None else input_stream
    try:
        hook_input: Any = json.load(stream)
    except json.JSONDecodeError as error:
        raise ValueError("Hook input must be a JSON object.") from error
    if not isinstance(hook_input, dict):
        raise ValueError("Hook input must be a JSON object.")

    raw_transcript = hook_input.get("transcript_path")
    if not isinstance(raw_transcript, str) or not raw_transcript.strip():
        raise ValueError("Hook input does not include transcript_path.")
    transcript = Path(raw_transcript).expanduser().resolve()
    if not transcript.is_file():
        raise ValueError(f"Hook transcript does not exist: {transcript}")

    provider_name, root = _history_location(provider_name, transcript, environment)
    provider = get_provider(provider_name)
    command = [
        sys.executable,
        "-m",
        "msync",
        "upload",
        "--dir",
        str(root),
        "--transcript",
        str(transcript),
        "--wait-for-transcript",
        "--provider",
        provider.name,
    ]
    _spawn_detached(command, environment)
    return True


def wait_for_transcript_stable(
    transcript: Path,
    *,
    quiet_seconds: float = _TRANSCRIPT_QUIET_SECONDS,
    timeout_seconds: float = _TRANSCRIPT_WAIT_TIMEOUT_SECONDS,
    poll_seconds: float = _TRANSCRIPT_POLL_SECONDS,
) -> None:
    """Wait in the detached worker until a transcript has stopped changing."""

    if quiet_seconds <= 0 or timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("Transcript wait intervals must be greater than zero.")

    signature = _transcript_signature(transcript)
    started_at = time.monotonic()
    unchanged_since = started_at
    while True:
        now = time.monotonic()
        quiet_remaining = quiet_seconds - (now - unchanged_since)
        timeout_remaining = timeout_seconds - (now - started_at)
        if quiet_remaining <= 0 or timeout_remaining <= 0:
            return

        time.sleep(min(poll_seconds, quiet_remaining, timeout_remaining))
        current_signature = _transcript_signature(transcript)
        if current_signature != signature:
            signature = current_signature
            unchanged_since = time.monotonic()


def _transcript_signature(transcript: Path) -> tuple[int, int]:
    stat = transcript.stat()
    return stat.st_size, stat.st_mtime_ns


def _history_location(
    provider_name: str | None,
    transcript: Path,
    environ: Mapping[str, str],
) -> tuple[str, Path]:
    if provider_name is None:
        provider_name = _provider_from_transcript(transcript, environ)
    else:
        provider_name = get_provider(provider_name).name

    search_directories = _PROVIDER_TRANSCRIPT_DIRECTORIES[provider_name]
    for parent in transcript.parents:
        if parent.name in search_directories:
            return provider_name, parent.parent

    environment_key = _PROVIDER_ROOT_ENV[provider_name]
    configured_root = environ.get(environment_key)
    if configured_root:
        root = Path(configured_root).expanduser().resolve()
        if transcript.is_relative_to(root):
            return provider_name, root
    raise ValueError(
        f"Could not find the {provider_name} history root for transcript: {transcript}"
    )


def _provider_from_transcript(transcript: Path, environ: Mapping[str, str]) -> str:
    for parent in transcript.parents:
        for candidate, search_directories in _PROVIDER_TRANSCRIPT_DIRECTORIES.items():
            if parent.name in search_directories:
                return candidate
    for candidate, environment_key in _PROVIDER_ROOT_ENV.items():
        configured_root = environ.get(environment_key)
        if configured_root and transcript.is_relative_to(
            Path(configured_root).expanduser().resolve()
        ):
            return candidate
    raise ValueError(f"Could not detect a history provider for transcript: {transcript}")


def _spawn_detached(command: list[str], environ: Mapping[str, str]) -> None:
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": dict(environ),
        "close_fds": True,
    }
    if os.name == "nt":  # pragma: no cover - exercised on Windows.
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)
