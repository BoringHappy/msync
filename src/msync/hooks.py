"""Non-blocking upload launcher for Claude Code and Codex lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from msync.providers import HistoryProvider, get_provider

_PROVIDER_ROOT_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
}
_PROVIDER_TRANSCRIPT_DIRECTORIES = {
    "claude": frozenset({"projects"}),
    "codex": frozenset({"sessions", "archived_sessions"}),
}
_TRANSCRIPT_QUIET_SECONDS = 2.0
_TRANSCRIPT_WAIT_TIMEOUT_SECONDS = 30.0
_TRANSCRIPT_POLL_SECONDS = 0.1
_CLOCK_EPSILON_SECONDS = 1e-9


def queue_session_upload(
    provider_name: str | None = None,
    *,
    input_stream: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Start a detached one-transcript upload and return without waiting for it."""

    environment = os.environ if environ is None else environ
    if not environment.get("MSYNC_ENDPOINT") or not environment.get("MSYNC_TOKEN"):
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
    expected_assistant_sha256 = _hook_assistant_sha256(hook_input, provider.name)
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
    ]
    if expected_assistant_sha256 is not None:
        command.extend(["--expected-assistant-sha256", expected_assistant_sha256])
    command.extend(["--provider", provider.name])
    _spawn_detached(command, environment)
    return True


def wait_for_transcript_stable(
    transcript: Path,
    *,
    provider: HistoryProvider | None = None,
    root: Path | None = None,
    expected_assistant_sha256: str | None = None,
    quiet_seconds: float = _TRANSCRIPT_QUIET_SECONDS,
    timeout_seconds: float = _TRANSCRIPT_WAIT_TIMEOUT_SECONDS,
    poll_seconds: float = _TRANSCRIPT_POLL_SECONDS,
) -> None:
    """Wait in the detached worker until a transcript has stopped changing."""

    if quiet_seconds <= 0 or timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("Transcript wait intervals must be greater than zero.")
    if expected_assistant_sha256 is not None:
        if provider is None or root is None:
            raise ValueError("Expected assistant message requires a provider and history root.")
        _validate_sha256(expected_assistant_sha256)

    signature = _transcript_signature(transcript)
    started_at = time.monotonic()
    unchanged_since = started_at
    expected_seen = expected_assistant_sha256 is None or _transcript_has_assistant_message(
        transcript,
        provider=provider,
        root=root,
        expected_sha256=expected_assistant_sha256,
    )
    while True:
        now = time.monotonic()
        quiet_remaining = quiet_seconds - (now - unchanged_since)
        timeout_remaining = timeout_seconds - (now - started_at)
        if expected_seen and quiet_remaining <= _CLOCK_EPSILON_SECONDS:
            return
        if timeout_remaining <= _CLOCK_EPSILON_SECONDS:
            if not expected_seen:
                raise TimeoutError("Final assistant message did not appear in the transcript.")
            return

        wait_seconds = min(poll_seconds, timeout_remaining)
        if expected_seen:
            wait_seconds = min(wait_seconds, quiet_remaining)
        time.sleep(wait_seconds)
        current_signature = _transcript_signature(transcript)
        if current_signature != signature:
            signature = current_signature
            unchanged_since = time.monotonic()
            if expected_assistant_sha256 is not None:
                expected_seen = _transcript_has_assistant_message(
                    transcript,
                    provider=provider,
                    root=root,
                    expected_sha256=expected_assistant_sha256,
                )


def _transcript_signature(transcript: Path) -> tuple[int, int]:
    stat = transcript.stat()
    return stat.st_size, stat.st_mtime_ns


def _hook_assistant_sha256(hook_input: dict[str, Any], provider_name: str) -> str | None:
    if provider_name != "claude":
        return None
    message = hook_input.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        return None
    return _assistant_message_sha256(message)


def _transcript_has_assistant_message(
    transcript: Path,
    *,
    provider: HistoryProvider | None,
    root: Path | None,
    expected_sha256: str,
) -> bool:
    assert provider is not None
    assert root is not None
    try:
        conversation = provider.read(transcript, root)
    except OSError, ValueError:
        return False

    assistant_events = [event for event in conversation.events if event.role == "assistant"]
    if not assistant_events:
        return False
    latest = assistant_events[-1]
    candidates = [latest.searchable_text, *(part.text for part in latest.parts)]
    return any(
        _assistant_message_sha256(candidate) == expected_sha256
        for candidate in candidates
        if candidate
    )


def _assistant_message_sha256(message: str) -> str:
    normalized = message.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _validate_sha256(value: str) -> None:
    if len(value) != 64:
        raise ValueError("Expected assistant message digest must be a SHA-256 value.")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("Expected assistant message digest must be a SHA-256 value.") from error


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
