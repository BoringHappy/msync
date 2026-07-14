from __future__ import annotations

import json
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from typer.testing import CliRunner

from msync.cli import app
from msync.providers import get_provider
from msync.schemas.claude import ClaudeRecord
from msync.schemas.codex import CodexRolloutLine
from msync.synchronization import MANIFEST_NAME


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _claude_records() -> list[dict[str, object]]:
    return [
        {
            "type": "user",
            "uuid": "claude-message-1",
            "sessionId": "claude-session",
            "parentUuid": None,
            "timestamp": "2026-07-14T10:00:00Z",
            "cwd": "/work/claude-project",
            "message": {"role": "user", "content": "Question from Claude"},
        },
        {
            "type": "assistant",
            "uuid": "claude-message-2",
            "sessionId": "claude-session",
            "parentUuid": "claude-message-1",
            "timestamp": "2026-07-14T10:00:01Z",
            "cwd": "/work/claude-project",
            "message": {
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "Answer from Claude"}],
            },
        },
    ]


def _codex_records() -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-07-14T11:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": "019f61a0-0000-7000-8000-000000000001",
                "id": "019f61a0-0000-7000-8000-000000000001",
                "timestamp": "2026-07-14T11:00:00Z",
                "cwd": "/work/codex-project",
                "originator": "codex_cli_rs",
                "cli_version": "1.0.0",
                "source": "cli",
            },
        },
        {
            "timestamp": "2026-07-14T11:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Question from Codex"},
        },
        {
            "timestamp": "2026-07-14T11:00:02Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Answer from Codex"},
        },
    ]


def test_sync_merges_both_platforms_into_native_resumable_histories(tmp_path: Path) -> None:
    claude_root = tmp_path / ".claude"
    codex_root = tmp_path / ".codex"
    claude_path = claude_root / "projects/-work-claude-project/claude-session.jsonl"
    codex_path = (
        codex_root
        / "sessions/2026/07/14"
        / "rollout-2026-07-14T11-00-00-019f61a0-0000-7000-8000-000000000001.jsonl"
    )
    _write_jsonl(claude_path, _claude_records())
    _write_jsonl(codex_path, _codex_records())
    original_claude = claude_path.read_bytes()
    original_codex = codex_path.read_bytes()
    database = tmp_path / "sync.sqlite"

    first = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(claude_root),
            "--dir",
            str(codex_root),
            "--database",
            str(database),
        ],
    )

    assert first.exit_code == 0, first.output
    assert first.output.count("Sync complete") == 2
    assert claude_path.read_bytes() == original_claude
    assert codex_path.read_bytes() == original_codex

    claude_generated = _generated_paths(claude_root)
    codex_generated = _generated_paths(codex_root)
    assert len(claude_generated) == 1
    assert len(codex_generated) == 1
    assert stat.S_IMODE(claude_generated[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(codex_generated[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((claude_root / MANIFEST_NAME).stat().st_mode) == 0o600
    assert stat.S_IMODE((codex_root / MANIFEST_NAME).stat().st_mode) == 0o600
    assert claude_generated[0].parent.name == "-work-codex-project"
    assert codex_generated[0].parent.relative_to(codex_root).parts[:4] == (
        "sessions",
        "2026",
        "07",
        "14",
    )

    claude_values = [
        ClaudeRecord.model_validate_json(line)
        for line in claude_generated[0].read_text().splitlines()
    ]
    codex_values = [
        CodexRolloutLine.model_validate_json(line)
        for line in codex_generated[0].read_text().splitlines()
    ]
    assert [record.type for record in claude_values] == ["user", "assistant"]
    assert codex_values[0].type == "session_meta"
    assert {record.type for record in codex_values[1:]} == {"response_item", "event_msg"}

    claude_conversation = get_provider("claude").read(claude_generated[0], claude_root)
    codex_conversation = get_provider("codex").read(codex_generated[0], codex_root)
    assert claude_conversation.title == "Question from Codex"
    assert [event.searchable_text for event in claude_conversation.events] == [
        "Question from Codex",
        "Answer from Codex",
    ]
    assert codex_conversation.title == "Question from Claude"
    assert [
        event.searchable_text
        for event in codex_conversation.events
        if event.visibility == "display"
    ] == ["Question from Claude", "Answer from Claude"]

    second = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(claude_root),
            "--dir",
            str(codex_root),
            "--database",
            str(database),
        ],
    )

    assert second.exit_code == 0, second.output
    assert len(re.findall(r"Native histories unchanged\s+1", second.output)) == 2
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM locations").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (2,)


def test_sync_can_generate_an_explicit_provider_location_from_archive(tmp_path: Path) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/2026/07/14/rollout-source.jsonl"
    _write_jsonl(codex_path, _codex_records())
    database = tmp_path / "sync.sqlite"
    upload = CliRunner().invoke(
        app,
        ["upload", "--dir", str(codex_root), "--database", str(database)],
    )
    assert upload.exit_code == 0, upload.output
    destination = tmp_path / "neutral-output"

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(destination),
            "--provider",
            "claude",
            "--database",
            str(database),
        ],
    )

    assert result.exit_code == 0, result.output
    generated = _generated_paths(destination)
    assert len(generated) == 1
    assert get_provider("claude").read(generated[0], destination).title == "Question from Codex"


def test_sync_does_not_overwrite_a_continued_export(tmp_path: Path) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/2026/07/14/rollout-source.jsonl"
    _write_jsonl(codex_path, _codex_records())
    destination = tmp_path / ".claude"
    database = tmp_path / "sync.sqlite"
    arguments = [
        "sync",
        "--dir",
        str(codex_root),
        "--dir",
        str(destination),
        "--provider",
        "codex",
        "--provider",
        "claude",
        "--database",
        str(database),
    ]
    first = CliRunner().invoke(app, arguments)
    assert first.exit_code == 0, first.output
    generated = _generated_paths(destination)[0]
    continued = generated.read_bytes() + b'{"type":"last-prompt","sessionId":"continued"}\n'
    generated.write_bytes(continued)

    second = CliRunner().invoke(app, arguments)

    assert second.exit_code == 0, second.output
    assert re.search(r"Continued exports protected\s+1", second.output)
    assert generated.read_bytes() == continued


def _generated_paths(root: Path) -> list[Path]:
    manifest = json.loads((root / MANIFEST_NAME).read_text())
    return sorted(root / relative_path for relative_path in manifest["files"])
