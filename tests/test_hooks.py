from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from msync.hooks import queue_session_upload, wait_for_transcript_stable
from msync.providers import get_provider


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
        "MSYNC_TOKEN": "secret-token",
    }

    hook_input = {
        "session_id": "hook-session",
        "transcript_path": str(transcript),
        "hook_event_name": "Stop",
    }
    if provider == "claude":
        hook_input["last_assistant_message"] = "Completed response"

    queued = queue_session_upload(
        input_stream=io.StringIO(json.dumps(hook_input)),
        environ=environment,
    )

    assert queued is True
    assert len(captured) == 1
    command, options = captured[0]
    expected_command = [
        sys.executable,
        "-m",
        "msync",
        "upload",
        "--dir",
        str(root.resolve()),
        "--transcript",
        str(transcript.resolve()),
        "--wait-for-transcript",
    ]
    if provider == "claude":
        expected_command.extend(
            [
                "--expected-assistant-sha256",
                hashlib.sha256(b"Completed response").hexdigest(),
            ]
        )
    expected_command.extend(["--provider", provider])
    assert command == expected_command
    assert "Completed response" not in command
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


def test_detached_upload_waits_for_delayed_transcript_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".claude"
    transcript = root / "projects/-work/session.jsonl"
    transcript.parent.mkdir(parents=True)
    initial_record = {
        "type": "user",
        "sessionId": "session",
        "uuid": "user-1",
        "timestamp": "2026-07-18T09:00:00Z",
        "message": {"role": "user", "content": "Question"},
    }
    final_message = "Final assistant message"
    final_record = {
        "type": "assistant",
        "sessionId": "session",
        "uuid": "assistant-1",
        "timestamp": "2026-07-18T09:00:01Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": final_message}],
        },
    }
    initial_content = json.dumps(initial_record) + "\n"
    transcript.write_text(initial_content)
    clock = {"now": 0.0}
    final_write = {"done": False}

    def fake_sleep(seconds: float) -> None:
        clock["now"] = round(clock["now"] + seconds, 10)
        if clock["now"] >= 0.5 and not final_write["done"]:
            transcript.write_text(initial_content + json.dumps(final_record) + "\n")
            final_write["done"] = True

    monkeypatch.setattr("msync.hooks.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("msync.hooks.time.sleep", fake_sleep)

    wait_for_transcript_stable(
        transcript,
        provider=get_provider("claude"),
        root=root.resolve(),
        expected_assistant_sha256=hashlib.sha256(final_message.encode()).hexdigest(),
        quiet_seconds=0.2,
        timeout_seconds=2.0,
        poll_seconds=0.1,
    )

    assert final_write["done"] is True
    assert clock["now"] >= 0.7
    assert final_message in transcript.read_text()


def test_detached_upload_rejects_missing_final_assistant_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".claude"
    transcript = root / "projects/-work/session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "session",
                "uuid": "user-1",
                "timestamp": "2026-07-18T09:00:00Z",
                "message": {"role": "user", "content": "Question"},
            }
        )
        + "\n"
    )
    clock = {"now": 0.0}

    monkeypatch.setattr("msync.hooks.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "msync.hooks.time.sleep",
        lambda seconds: clock.__setitem__("now", round(clock["now"] + seconds, 10)),
    )

    with pytest.raises(TimeoutError, match="Final assistant message"):
        wait_for_transcript_stable(
            transcript,
            provider=get_provider("claude"),
            root=root.resolve(),
            expected_assistant_sha256=hashlib.sha256(b"Missing response").hexdigest(),
            quiet_seconds=0.1,
            timeout_seconds=0.4,
            poll_seconds=0.1,
        )

    assert clock["now"] >= 0.4
