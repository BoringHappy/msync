from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from click import unstyle
from typer.testing import CliRunner

from msync.cli import app
from msync.database import Archive
from msync.providers import get_provider
from msync.remote import UPLOAD_CONTENT_TYPE, RemoteUploadMetadata


def _write_codex_transcript(
    root: Path,
    *,
    session_id: str = "cli-session",
    filename: str = "rollout.jsonl",
) -> Path:
    transcript = root / "sessions" / filename
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-14T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": "/tmp"},
            }
        )
        + "\n"
    )
    return transcript


def _archive_transcripts(root: Path, database: Path, provider_name: str = "codex") -> None:
    provider = get_provider(provider_name)
    resolved_root = root.resolve()
    with Archive(database) as archive:
        archive.upload(
            root=resolved_root,
            provider=provider,
            transcripts=provider.discover(resolved_root),
        )


def test_upload_requires_url(tmp_path: Path) -> None:
    root = tmp_path / ".codex_custom"
    _write_codex_transcript(root)

    result = CliRunner().invoke(app, ["upload", "--dir", str(root)])

    assert result.exit_code == 2
    assert "Missing option '--url'" in unstyle(result.output)


def test_upload_rejects_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / ".codex_empty"
    root.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--url",
            "https://history.example",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 1
    assert "No conversation transcripts found" in result.output


def test_upload_rejects_empty_hostname(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-14T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": "cli-session", "cwd": "/tmp"},
            }
        )
        + "\n"
    )

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--url",
            "https://history.example",
            "--token",
            "secret",
            "--hostname",
            " ",
        ],
    )

    assert result.exit_code == 1
    assert "Location hostname must not be empty" in result.output


def test_upload_accepts_explicit_claude_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "custom-history"
    transcript = root / "projects/-work/session.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "message-1",
                "sessionId": "claude-session",
                "timestamp": "2026-07-14T12:00:00Z",
                "message": {"role": "user", "content": "hello Claude"},
            }
        )
        + "\n"
    )
    captured_provider: list[str] = []

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        content: Iterator[bytes],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del headers, timeout
        body = b"".join(content)
        metadata_length = int.from_bytes(body[:4], byteorder="big")
        metadata = RemoteUploadMetadata.model_validate_json(body[4 : 4 + metadata_length])
        captured_provider.append(metadata.provider)
        return httpx.Response(
            200,
            json={
                "location_id": 1,
                "scanned": 1,
                "imported": 1,
                "updated": 0,
                "unchanged": 0,
                "duplicates": 0,
                "events": 1,
                "message_parts": 0,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("msync.cli.httpx.post", fake_post)

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--provider",
            "claude",
            "--url",
            "https://history.example",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "claude" in result.output
    assert captured_provider == ["claude"]


def test_upload_sends_native_transcripts_to_remote_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex-remote"
    transcript = _write_codex_transcript(root, session_id="remote-session")
    second_transcript = _write_codex_transcript(
        root,
        session_id="second-remote-session",
        filename="second.jsonl",
    )
    captured: list[dict[str, Any]] = []

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        content: Iterator[bytes],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        captured.append(
            {
                "url": url,
                "headers": headers,
                "body": b"".join(content),
                "timeout": timeout,
            }
        )
        return httpx.Response(
            200,
            json={
                "location_id": 12,
                "scanned": 1,
                "imported": 1,
                "updated": 0,
                "unchanged": 0,
                "duplicates": 0,
                "events": 1,
                "message_parts": 0,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("msync.cli.httpx.post", fake_post)
    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--url",
            "https://history.example/msync/",
            "--token",
            "alice-token",
            "--hostname",
            "alice-laptop",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Session pre-check: 2 passed, 0 failed." in result.output
    assert "Upload complete" in result.output
    assert "Server" in result.output
    assert "https://history.example/msync" in result.output
    assert "alice-token" not in result.output
    assert re.search(r"Transcripts\s+2", result.output)
    assert len(captured) == 2
    expected_paths = ["sessions/rollout.jsonl", "sessions/second.jsonl"]
    expected_content = [transcript.read_bytes(), second_transcript.read_bytes()]
    for request, relative_path, transcript_content in zip(
        captured,
        expected_paths,
        expected_content,
        strict=True,
    ):
        assert request["url"] == "https://history.example/msync/api/upload"
        assert request["headers"]["Authorization"] == "Bearer alice-token"
        assert request["headers"]["Content-Type"] == UPLOAD_CONTENT_TYPE
        assert int(request["headers"]["Content-Length"]) == len(request["body"])
        assert request["timeout"].read is None
        assert request["timeout"].write is None
        metadata_length = int.from_bytes(request["body"][:4], byteorder="big")
        metadata = RemoteUploadMetadata.model_validate_json(
            request["body"][4 : 4 + metadata_length]
        )
        assert metadata.provider == "codex"
        assert metadata.hostname == "alice-laptop"
        assert metadata.root_path == str(root.resolve())
        assert metadata.relative_path == relative_path
        assert request["body"][4 + metadata_length :] == transcript_content


def test_upload_sends_only_selected_transcript_with_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex"
    _write_codex_transcript(root, session_id="first", filename="first.jsonl")
    selected = _write_codex_transcript(root, session_id="selected", filename="selected.jsonl")
    captured_bodies: list[bytes] = []

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        content: Iterator[bytes],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del timeout
        captured_bodies.append(b"".join(content))
        assert url == "https://history.example/api/upload"
        assert headers["Authorization"] == "Bearer env-token"
        return httpx.Response(
            200,
            json={
                "location_id": 12,
                "scanned": 1,
                "imported": 0,
                "updated": 1,
                "unchanged": 0,
                "duplicates": 0,
                "events": 1,
                "message_parts": 0,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("msync.cli.httpx.post", fake_post)
    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--transcript",
            str(selected),
            "--provider",
            "codex",
        ],
        env={
            "MSYNC_UPLOAD_URL": "https://history.example",
            "MSYNC_UPLOAD_TOKEN": "env-token",
        },
    )

    assert result.exit_code == 0, result.output
    assert re.search(r"Transcripts\s+1", result.output)
    assert re.search(r"Updated\s+1", result.output)
    assert len(captured_bodies) == 1
    metadata_length = int.from_bytes(captured_bodies[0][:4], byteorder="big")
    metadata = RemoteUploadMetadata.model_validate_json(captured_bodies[0][4 : 4 + metadata_length])
    assert metadata.relative_path == "sessions/selected.jsonl"
    assert captured_bodies[0][4 + metadata_length :] == selected.read_bytes()


def test_hook_upload_waits_for_selected_transcript_before_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex"
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()
    calls: list[str] = []

    def fake_wait(path: Path) -> None:
        assert path == transcript.resolve()
        calls.append("wait")
        _write_codex_transcript(root)

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        content: Iterator[bytes],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del headers, content, timeout
        calls.append("post")
        return httpx.Response(
            200,
            json={
                "location_id": 1,
                "scanned": 1,
                "imported": 1,
                "updated": 0,
                "unchanged": 0,
                "duplicates": 0,
                "events": 1,
                "message_parts": 0,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("msync.cli.wait_for_transcript_stable", fake_wait)
    monkeypatch.setattr("msync.cli.httpx.post", fake_post)

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--transcript",
            str(transcript),
            "--wait-for-transcript",
            "--provider",
            "codex",
            "--url",
            "https://history.example",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["wait", "post"]


def test_upload_rejects_selected_transcript_outside_history_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex"
    _write_codex_transcript(root)
    outside = _write_codex_transcript(tmp_path / "other", filename="outside.jsonl")

    def unexpected_post(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("outside transcript reached the network")

    monkeypatch.setattr("msync.cli.httpx.post", unexpected_post)
    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--transcript",
            str(outside),
            "--provider",
            "codex",
            "--url",
            "https://history.example",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 1
    assert "Transcript must be contained in --dir" in result.output


def test_upload_reports_failed_session_prechecks_and_uploads_verified_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex"
    valid = _write_codex_transcript(root)
    malformed = root / "sessions/malformed.jsonl"
    malformed.write_text('{"type": "session_meta"\n')
    empty = root / "sessions/empty.jsonl"
    empty.touch()
    missing_timestamp = root / "sessions/missing-timestamp.jsonl"
    missing_timestamp.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "missing-timestamp", "cwd": "/tmp"},
            }
        )
        + "\n"
    )
    uploaded_paths: list[str] = []

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        content: Iterator[bytes],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        del headers, timeout
        body = b"".join(content)
        metadata_length = int.from_bytes(body[:4], byteorder="big")
        metadata = RemoteUploadMetadata.model_validate_json(body[4 : 4 + metadata_length])
        uploaded_paths.append(metadata.relative_path)
        return httpx.Response(
            200,
            json={
                "location_id": 1,
                "scanned": 1,
                "imported": 1,
                "updated": 0,
                "unchanged": 0,
                "duplicates": 0,
                "events": 1,
                "message_parts": 0,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("msync.cli.httpx.post", fake_post)

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--url",
            "https://history.example",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 1
    assert "Session pre-check: 1 passed, 3 failed." in result.output
    assert "sessions/empty.jsonl" in result.output
    assert "transcript contains no JSON records" in result.output
    assert "sessions/malformed.jsonl" in result.output
    assert "line 1:" in result.output
    assert "sessions/missing-timestamp.jsonl" in result.output
    assert "session contains no event timestamps" in result.output
    assert "Upload incomplete" in result.output
    assert re.search(r"Sessions failed\s+3", result.output)
    assert uploaded_paths == [valid.relative_to(root).as_posix()]


def test_upload_does_not_contact_server_when_no_session_passes_precheck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex"
    transcript = root / "sessions/empty.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    def unexpected_post(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("failed pre-check reached the network")

    monkeypatch.setattr("msync.cli.httpx.post", unexpected_post)

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--url",
            "https://history.example",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 1
    assert "Session pre-check: 0 passed, 1 failed." in result.output
    assert "Session not uploaded: sessions/empty.jsonl" in result.output
    assert "No sessions passed the pre-upload verification" in result.output


def test_upload_requires_token(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    _write_codex_transcript(root)

    result = CliRunner().invoke(
        app,
        ["upload", "--dir", str(root), "--url", "https://history.example"],
    )

    assert result.exit_code == 1
    assert "--url requires --token" in result.output


def test_upload_no_longer_accepts_database(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    _write_codex_transcript(root)

    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--url",
            "https://history.example",
            "--token",
            "secret",
            "--database",
            "archive.sqlite",
        ],
    )

    assert result.exit_code == 2
    assert "No such option: --database" in unstyle(result.output)


def test_upload_rejects_oversized_transcript_before_network_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex"
    transcript = _write_codex_transcript(root)
    monkeypatch.setattr("msync.cli.UPLOAD_TRANSCRIPT_MAX_BYTES", transcript.stat().st_size - 1)

    def unexpected_post(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("oversized transcript reached the network")

    monkeypatch.setattr("msync.cli.httpx.post", unexpected_post)
    result = CliRunner().invoke(
        app,
        [
            "upload",
            "--dir",
            str(root),
            "--url",
            "https://history.example",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 1
    assert "Transcript exceeds the 256 MiB remote upload limit" in result.output


def test_upgrade_command_migrates_archive_and_reports_steps(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    result = CliRunner().invoke(
        app,
        ["upgrade", "--database", str(database), "--lock-timeout", "1"],
    )

    assert result.exit_code == 0, result.output
    assert "Upgrading database schema 5 → 6" in result.output
    assert "Upgrading database schema 6 → 7" in result.output
    assert "Upgrading database schema 7 → 8" in result.output
    assert "Upgrading database schema 8 → 9" in result.output
    assert "Database schema upgrade complete: 5 → 9" in result.output
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone() == ("9",)


def test_upgrade_command_reports_current_schema(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass

    result = CliRunner().invoke(app, ["upgrade", "--database", str(database)])

    assert result.exit_code == 0, result.output
    assert "Database schema is current at version 9" in result.output


def test_upgrade_command_reports_concurrent_transaction(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    with closing(sqlite3.connect(database)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        result = CliRunner().invoke(
            app,
            ["upgrade", "--database", str(database), "--lock-timeout", "1"],
        )
        blocker.rollback()

    assert result.exit_code == 1
    assert "blocked by another" in result.output
    assert "transaction." in result.output
    assert "Stop or finish other msync uploads" in result.output


def test_regular_command_requires_explicit_schema_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    with Archive(database):
        pass
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()

    result = CliRunner().invoke(app, ["sample", "1", "--database", str(database)])

    assert result.exit_code == 1
    assert "must be upgraded to 9" in result.output
    assert "msync upgrade --database <database>" in result.output


def test_search_command_finds_text_with_like_query(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "search-session", "cwd": "/tmp"},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "Find the blue widget today",
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    database = tmp_path / "search.sqlite"
    _archive_transcripts(root, database)

    result = CliRunner().invoke(
        app,
        ["search", "blue widget", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "Search results (1)" in result.output
    assert "codex" in result.output
    assert "Find the blue widget today" in result.output


def test_search_command_reports_no_matches(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"

    result = CliRunner().invoke(
        app,
        ["search", "missing", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "No matches found for missing." in result.output


def test_sample_command_limits_random_messages(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript = root / "sessions/rollout.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "sample-session", "cwd": "/tmp"},
                    }
                )
            ]
            + [
                json.dumps(
                    {
                        "timestamp": f"2026-07-14T12:00:0{sequence}Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": f"Sample message {sequence}",
                        },
                    }
                )
                for sequence in range(1, 4)
            ]
        )
        + "\n"
    )
    database = tmp_path / "sample.sqlite"
    _archive_transcripts(root, database)

    result = CliRunner().invoke(
        app,
        ["sample", "2", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "Samples (2)" in result.output
    assert " 1  codex " in result.output
    assert " 2  codex " in result.output
    assert result.output.count("─") > 2
    assert result.output.count("Conversation") == 2
    assert result.output.count("Role         user") == 2


def test_sample_command_reports_empty_archive(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"

    result = CliRunner().invoke(
        app,
        ["sample", "3", "--database", str(database)],
    )

    assert result.exit_code == 0, result.output
    assert "No archived messages found." in result.output
