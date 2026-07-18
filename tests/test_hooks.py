from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from msync.hooks import queue_session_upload


@pytest.mark.parametrize(
    ("provider", "relative_path"),
    [
        ("claude", "projects/-work/session.jsonl"),
        ("codex", "sessions/2026/07/18/rollout.jsonl"),
        ("codex", "archived_sessions/rollout.jsonl"),
    ],
)
def test_hook_queues_one_native_transcript_without_credentials_in_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    relative_path: str,
) -> None:
    root = tmp_path / f".{provider}"
    transcript = root / relative_path
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    captured: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(command: list[str], **options: Any) -> object:
        captured.append((command, options))
        return object()

    monkeypatch.setattr("msync.hooks.subprocess.Popen", fake_popen)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "MSYNC_UPLOAD_URL": "https://history.example",
        "MSYNC_UPLOAD_TOKEN": "secret-token",
    }

    queued = queue_session_upload(
        input_stream=io.StringIO(
            json.dumps(
                {
                    "session_id": "hook-session",
                    "transcript_path": str(transcript),
                    "hook_event_name": "Stop",
                }
            )
        ),
        environ=environment,
    )

    assert queued is True
    assert len(captured) == 1
    command, options = captured[0]
    assert command == [
        sys.executable,
        "-m",
        "msync",
        "upload",
        "--dir",
        str(root.resolve()),
        "--transcript",
        str(transcript.resolve()),
        "--provider",
        provider,
    ]
    assert "secret-token" not in command
    assert "https://history.example" not in command
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stdout"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.DEVNULL
    assert options["env"] == environment
    if os.name == "nt":
        assert options["creationflags"]
    else:
        assert options["start_new_session"] is True


def test_hook_uses_configured_root_for_custom_claude_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "custom-claude"
    transcript = root / "history" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "msync.hooks.subprocess.Popen",
        lambda command, **options: commands.append(command),
    )

    queued = queue_session_upload(
        input_stream=io.StringIO(json.dumps({"transcript_path": str(transcript)})),
        environ={
            "CLAUDE_CONFIG_DIR": str(root),
            "MSYNC_UPLOAD_URL": "https://history.example",
            "MSYNC_UPLOAD_TOKEN": "secret-token",
        },
    )

    assert queued is True
    assert commands[0][-2:] == ["--provider", "claude"]
    assert commands[0][commands[0].index("--dir") + 1] == str(root.resolve())


def test_hook_is_silent_noop_without_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_popen(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("unconfigured hook launched an upload")

    monkeypatch.setattr("msync.hooks.subprocess.Popen", unexpected_popen)

    assert queue_session_upload(input_stream=io.StringIO("not json"), environ={}) is False


def test_hook_rejects_transcript_without_native_or_configured_root(tmp_path: Path) -> None:
    transcript = tmp_path / "history" / "session.jsonl"
    transcript.parent.mkdir()
    transcript.write_text("{}\n")

    with pytest.raises(ValueError, match="Could not detect a history provider"):
        queue_session_upload(
            input_stream=io.StringIO(json.dumps({"transcript_path": str(transcript)})),
            environ={
                "MSYNC_UPLOAD_URL": "https://history.example",
                "MSYNC_UPLOAD_TOKEN": "secret-token",
            },
        )
