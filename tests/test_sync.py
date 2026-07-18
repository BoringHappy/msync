from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import msync.synchronization as synchronization
from msync.cli import app
from msync.database import Archive, RemoteTranscript, UploadResult
from msync.providers import get_provider
from msync.schemas.claude import ClaudeRecord
from msync.schemas.codex import CodexRolloutLine
from msync.server import ServerAccount, create_app
from msync.synchronization import MANIFEST_NAME, managed_transcript_logical_session_id

REMOTE_URL = "https://history.example"
REMOTE_TOKEN = "sync-token"
REMOTE_ACCOUNT = "sync-user"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _archive_transcripts(database: Path, root: Path, provider_name: str = "codex") -> UploadResult:
    provider = get_provider(provider_name)
    resolved_root = root.resolve()
    transcripts = provider.discover(resolved_root)
    logical_session_ids = {
        path.relative_to(resolved_root).as_posix(): logical_session_id
        for path in transcripts
        if (
            logical_session_id := managed_transcript_logical_session_id(
                resolved_root,
                path,
                provider=provider,
            )
        )
        is not None
    }
    with Archive(database) as archive:
        return archive.upload(
            root=resolved_root,
            provider=provider,
            transcripts=transcripts,
            account_username=REMOTE_ACCOUNT,
            logical_session_ids=logical_session_ids,
        )


@pytest.fixture
def remote_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Route real CLI HTTP calls through an authenticated temporary server archive."""

    database = tmp_path / "remote.sqlite"
    web_app = create_app(
        database,
        accounts=(ServerAccount(REMOTE_ACCOUNT, "password", REMOTE_TOKEN),),
    )
    with TestClient(web_app) as client:

        def remote_get(
            url: str,
            *,
            params: dict[str, str | int] | None,
            headers: dict[str, str],
            timeout: httpx.Timeout,
        ) -> httpx.Response:
            del timeout
            target = httpx.URL(url)
            assert f"{target.scheme}://{target.host}" == REMOTE_URL
            return client.get(target.path, params=params, headers=headers)

        def remote_post(
            url: str,
            *,
            headers: dict[str, str],
            content: Iterator[bytes],
            timeout: httpx.Timeout,
        ) -> httpx.Response:
            del timeout
            target = httpx.URL(url)
            assert f"{target.scheme}://{target.host}" == REMOTE_URL
            return client.post(target.path, headers=headers, content=b"".join(content))

        monkeypatch.setattr("msync.cli.httpx.get", remote_get)
        monkeypatch.setattr("msync.cli.httpx.post", remote_post)
        yield database


def _remote_options() -> list[str]:
    return ["--url", REMOTE_URL, "--token", REMOTE_TOKEN]


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


def test_sync_merges_both_platforms_into_native_resumable_histories(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
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
    first = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(claude_root),
            "--dir",
            str(codex_root),
        ],
        env={"MSYNC_ENDPOINT": REMOTE_URL, "MSYNC_TOKEN": REMOTE_TOKEN},
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
    assert all(
        "msync" not in json.loads(line) for line in claude_generated[0].read_text().splitlines()
    )
    assert all(
        "msync" not in json.loads(line).get("payload", {})
        for line in codex_generated[0].read_text().splitlines()
    )
    for root, generated in (
        (claude_root, claude_generated[0]),
        (codex_root, codex_generated[0]),
    ):
        manifest = json.loads((root / MANIFEST_NAME).read_text())
        assert manifest["version"] == 2
        entry = manifest["files"][generated.relative_to(root).as_posix()]
        assert entry["external_id"]
        assert entry["logical_session_id"]
        assert entry["chat_sha256"]
        assert entry["writer_schema_version"] == 1

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
        ],
        env={"MSYNC_ENDPOINT": REMOTE_URL, "MSYNC_TOKEN": REMOTE_TOKEN},
    )

    assert second.exit_code == 0, second.output
    assert len(re.findall(r"Native histories unchanged\s+1", second.output)) == 2
    with closing(sqlite3.connect(remote_archive)) as connection:
        assert connection.execute("SELECT count(*) FROM locations").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (2,)


def test_sync_can_generate_an_explicit_provider_location_from_archive(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/2026/07/14/rollout-source.jsonl"
    _write_jsonl(codex_path, _codex_records())
    _archive_transcripts(remote_archive, codex_root)
    destination = tmp_path / "neutral-output"

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(destination),
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 0, result.output
    generated = _generated_paths(destination)
    assert len(generated) == 1
    assert get_provider("claude").read(generated[0], destination).title == "Question from Codex"


def test_sync_writes_same_path_history_from_another_host(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    destination = (tmp_path / ".codex").resolve()
    destination.mkdir()
    transcript = ("".join(json.dumps(record) + "\n" for record in _codex_records())).encode()
    with Archive(remote_archive) as archive:
        uploaded = archive.upload_remote(
            root_path=str(destination),
            display_name=destination.name,
            provider=get_provider("codex"),
            hostname="remote-host",
            account_username=REMOTE_ACCOUNT,
            transcripts=[
                RemoteTranscript(
                    relative_path="sessions/remote-host.jsonl",
                    content=transcript,
                )
            ],
        )
    assert uploaded.imported == 1

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(destination),
            "--provider",
            "codex",
            "--hostname",
            "local-host",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 0, result.output
    assert re.search(r"Native histories written\s+1", result.output)
    generated = _generated_paths(destination)
    assert len(generated) == 1
    assert get_provider("codex").read(generated[0], destination).title == "Question from Codex"


def test_sync_does_not_overwrite_a_continued_export(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/2026/07/14/rollout-source.jsonl"
    _write_jsonl(codex_path, _codex_records())
    destination = tmp_path / ".claude"
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
        *_remote_options(),
    ]
    first = CliRunner().invoke(app, arguments)
    assert first.exit_code == 0, first.output
    generated = _generated_paths(destination)[0]
    manifest_path = destination / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = 1
    generated_key = generated.relative_to(destination).as_posix()
    legacy_identity = json.dumps(
        ["codex", str(codex_root.resolve()), codex_path.relative_to(codex_root).as_posix()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    manifest["files"][generated_key]["sources"] = [
        hashlib.sha256(legacy_identity.encode()).hexdigest()
    ]
    manifest_path.write_text(json.dumps(manifest))
    records = [json.loads(line) for line in generated.read_text().splitlines()]
    continued_record = {
        "type": "user",
        "uuid": "continued-message",
        "sessionId": records[0]["sessionId"],
        "parentUuid": records[-1]["uuid"],
        "timestamp": "2026-07-14T11:00:03Z",
        "cwd": "/work/codex-project",
        "message": {"role": "user", "content": "Continued in Claude"},
    }
    continued = generated.read_bytes() + (json.dumps(continued_record) + "\n").encode()
    generated.write_bytes(continued)

    second = CliRunner().invoke(app, arguments)

    assert second.exit_code == 0, second.output
    assert re.search(r"Existing histories protected\s+1", second.output)
    assert generated.read_bytes() == continued
    assert json.loads(manifest_path.read_text())["version"] == 2


def test_changed_source_creates_new_session_without_overwriting_previous(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/2026/07/14/rollout-source.jsonl"
    _write_jsonl(codex_path, _codex_records())
    destination = tmp_path / ".claude"
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
        *_remote_options(),
    ]
    first = CliRunner().invoke(app, arguments)
    assert first.exit_code == 0, first.output
    previous = _generated_paths(destination)[0]
    previous_content = previous.read_bytes()

    records = _codex_records()
    records.append(
        {
            "timestamp": "2026-07-14T11:00:03Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "A later Codex turn"},
        }
    )
    _write_jsonl(codex_path, records)
    second = CliRunner().invoke(app, arguments)

    assert second.exit_code == 0, second.output
    generated = _generated_paths(destination)
    assert len(generated) == 2
    assert previous.read_bytes() == previous_content
    assert any("A later Codex turn" in path.read_text() for path in generated if path != previous)


def test_sync_reuses_a_managed_revision_from_an_older_path_scheme(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/2026/07/14/rollout-source.jsonl"
    _write_jsonl(codex_path, _codex_records())
    claude_root = tmp_path / ".claude"
    arguments = [
        "sync",
        "--dir",
        str(codex_root),
        "--dir",
        str(claude_root),
        "--provider",
        "codex",
        "--provider",
        "claude",
        *_remote_options(),
    ]
    first = CliRunner().invoke(app, arguments)
    assert first.exit_code == 0, first.output
    generated = _generated_paths(claude_root)[0]
    legacy = generated.with_name("legacy-source-key-session.jsonl")
    generated.rename(legacy)
    manifest_path = claude_root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    entry = manifest["files"].pop(generated.relative_to(claude_root).as_posix())
    manifest["files"][legacy.relative_to(claude_root).as_posix()] = entry
    manifest_path.write_text(json.dumps(manifest))

    second = CliRunner().invoke(app, arguments)

    assert second.exit_code == 0, second.output
    assert re.search(r"Native histories unchanged\s+1", second.output)
    assert _generated_paths(claude_root) == [legacy]


def test_same_provider_revisions_are_cloned_instead_of_conflicting(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    first_root = tmp_path / "codex-a"
    second_root = tmp_path / "codex-b"
    relative_path = Path("sessions/2026/07/14/rollout-shared.jsonl")
    first_path = first_root / relative_path
    second_path = second_root / relative_path
    first_records = _codex_records()
    second_records = _codex_records()
    second_records[1]["payload"] = {
        "type": "user_message",
        "message": "Question from the second Codex location",
    }
    _write_jsonl(first_path, first_records)
    _write_jsonl(second_path, second_records)
    first_original = first_path.read_bytes()
    second_original = second_path.read_bytes()

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(first_root),
            "--dir",
            str(second_root),
            "--provider",
            "codex",
            "--provider",
            "codex",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(re.findall(r"Path conflicts\s+0", result.output)) == 2
    assert first_path.read_bytes() == first_original
    assert second_path.read_bytes() == second_original
    for root in (first_root, second_root):
        assert len(_generated_paths(root)) == 1
        titles = {get_provider("codex").read(path, root).title for path in root.rglob("*.jsonl")}
        assert titles == {"Question from Codex", "Question from the second Codex location"}


def test_sync_rejects_symlinked_destination_components(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/2026/07/14/rollout-source.jsonl"
    _write_jsonl(codex_path, _codex_records())
    _archive_transcripts(remote_archive, codex_root)
    destination = tmp_path / ".claude"
    outside = tmp_path / "outside"
    destination.mkdir()
    outside.mkdir()
    (destination / "projects").symlink_to(outside, target_is_directory=True)

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(destination),
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 1
    assert "Refusing to write through a symlink" in result.output
    assert list(outside.rglob("*.jsonl")) == []


def test_sync_never_replaces_an_unmanaged_path_collision(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    codex_root = tmp_path / ".codex"
    _write_jsonl(codex_root / "sessions/source.jsonl", _codex_records())
    claude_root = tmp_path / ".claude"
    arguments = [
        "sync",
        "--dir",
        str(codex_root),
        "--dir",
        str(claude_root),
        "--provider",
        "codex",
        "--provider",
        "claude",
        *_remote_options(),
    ]
    first = CliRunner().invoke(app, arguments)
    assert first.exit_code == 0, first.output
    collision = _generated_paths(claude_root)[0]
    collision.write_text("".join(json.dumps(record) + "\n" for record in _claude_records()))
    user_bytes = collision.read_bytes()
    manifest_path = claude_root / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].pop(collision.relative_to(claude_root).as_posix())
    manifest_path.write_text(json.dumps(manifest))

    second = CliRunner().invoke(app, arguments)

    assert second.exit_code == 1
    assert "Sync incomplete" in second.output
    assert re.search(r"Path conflicts\s+1", second.output)
    assert "Not overwritten" in second.output
    assert collision.read_bytes() == user_bytes


def test_sync_rejects_an_unsupported_manifest_before_network_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / ".claude"
    destination.mkdir()
    manifest_path = destination / MANIFEST_NAME
    manifest_bytes = b'{"version":999,"files":{}}\n'
    manifest_path.write_bytes(manifest_bytes)

    def unexpected_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network must not be opened for an unsupported manifest")

    monkeypatch.setattr("msync.cli.httpx.get", unexpected_network)
    monkeypatch.setattr("msync.cli.httpx.post", unexpected_network)

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(destination),
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 1
    assert "Unsupported or invalid sync manifest" in result.output
    assert manifest_path.read_bytes() == manifest_bytes
    assert list(destination.rglob("*.jsonl")) == []


def test_sync_uses_atomic_create_when_a_target_appears_during_commit(
    tmp_path: Path,
    remote_archive: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_root = tmp_path / ".codex"
    _write_jsonl(codex_root / "sessions/source.jsonl", _codex_records())
    destination = tmp_path / "claude-output"
    user_bytes = b"concurrently-created user data\n"
    real_link = os.link
    raced_paths: list[Path] = []

    def race_link(source: str | Path, target: str | Path, **options: object) -> None:
        target_path = Path(target)
        if not raced_paths:
            target_path.write_bytes(user_bytes)
            raced_paths.append(target_path)
        real_link(source, target, **options)

    monkeypatch.setattr("msync.synchronization.os.link", race_link)
    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(codex_root),
            "--dir",
            str(destination),
            "--provider",
            "codex",
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 1
    assert raced_paths
    assert raced_paths[0].read_bytes() == user_bytes
    assert re.search(r"Path conflicts\s+1", result.output)


def test_sync_validates_every_generated_candidate_before_writing_any(
    tmp_path: Path,
    remote_archive: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_root = tmp_path / ".codex"
    first_records = _codex_records()
    second_records = _codex_records()
    second_session_id = "019f61a0-0000-7000-8000-000000000002"
    second_records[0]["payload"] = {
        **second_records[0]["payload"],
        "session_id": second_session_id,
        "id": second_session_id,
    }
    second_records[1]["payload"] = {
        "type": "user_message",
        "message": "A distinct second conversation",
    }
    _write_jsonl(codex_root / "sessions/first.jsonl", first_records)
    _write_jsonl(codex_root / "sessions/second.jsonl", second_records)
    destination = tmp_path / "claude-output"
    provider = get_provider("claude")
    real_encode = provider.encode_conversation
    calls = 0

    def encode_with_bad_second_identity(*args: object, **kwargs: object) -> bytes:
        nonlocal calls
        calls += 1
        content = real_encode(*args, **kwargs)
        if calls == 2:
            records = [json.loads(line) for line in content.splitlines()]
            for record in records:
                record["sessionId"] = "019f61a0-0000-7000-8000-ffffffffffff"
            return ("".join(json.dumps(record) + "\n" for record in records)).encode()
        return content

    monkeypatch.setattr(provider, "encode_conversation", encode_with_bad_second_identity)
    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(codex_root),
            "--dir",
            str(destination),
            "--provider",
            "codex",
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 1
    assert "session identity changed" in result.output
    assert list(destination.rglob("*.jsonl")) == []
    assert not (destination / MANIFEST_NAME).exists()


def test_sync_rolls_back_new_transcripts_when_manifest_commit_fails(
    tmp_path: Path,
    remote_archive: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_root = tmp_path / ".codex"
    _write_jsonl(codex_root / "sessions/source.jsonl", _codex_records())
    destination = tmp_path / "claude-output"
    real_write_manifest = synchronization._write_manifest

    def fail_destination_manifest(root: Path, manifest: dict[str, object]) -> None:
        if root == destination.resolve():
            raise OSError("simulated manifest failure")
        real_write_manifest(root, manifest)

    monkeypatch.setattr(synchronization, "_write_manifest", fail_destination_manifest)
    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(codex_root),
            "--dir",
            str(destination),
            "--provider",
            "codex",
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 1
    assert "simulated manifest failure" in result.output
    assert list(destination.rglob("*.jsonl")) == []
    assert list(destination.rglob(".msync-*")) == []


@pytest.mark.parametrize("unsafe_path", [Path("../outside.jsonl"), Path(MANIFEST_NAME)])
def test_sync_rejects_unsafe_provider_export_paths_before_writing(
    tmp_path: Path,
    remote_archive: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_path: Path,
) -> None:
    codex_root = tmp_path / ".codex"
    _write_jsonl(codex_root / "sessions/source.jsonl", _codex_records())
    destination = tmp_path / "claude-output"
    provider = get_provider("claude")
    monkeypatch.setattr(
        provider,
        "export_relative_path",
        lambda *args, **kwargs: unsafe_path,
    )

    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(codex_root),
            "--dir",
            str(destination),
            "--provider",
            "codex",
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 1
    assert "unsafe transcript path" in result.output
    assert not (tmp_path / "outside.jsonl").exists()
    assert list(destination.rglob("*.jsonl")) == []
    assert not (destination / MANIFEST_NAME).exists()


def test_sync_rejects_an_explicit_provider_that_conflicts_with_the_destination(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    root = tmp_path / ".claude"
    _write_jsonl(root / "projects/-work/session.jsonl", _claude_records())

    result = CliRunner().invoke(
        app,
        ["sync", "--dir", str(root), "--provider", "codex", *_remote_options()],
    )

    assert result.exit_code == 1
    normalized_output = " ".join(result.output.split())
    assert "contains claude history, not the requested codex provider" in normalized_output
    assert not (root / "sessions").exists()


def test_destination_lock_serializes_manifest_updates(
    tmp_path: Path,
    remote_archive: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_root = tmp_path / ".codex"
    first_records = _codex_records()
    second_records = _codex_records()
    second_session_id = "019f61a0-0000-7000-8000-000000000003"
    second_records[0]["payload"] = {
        **second_records[0]["payload"],
        "session_id": second_session_id,
        "id": second_session_id,
    }
    second_records[1]["payload"] = {
        "type": "user_message",
        "message": "Concurrent second conversation",
    }
    _write_jsonl(codex_root / "sessions/first.jsonl", first_records)
    _write_jsonl(codex_root / "sessions/second.jsonl", second_records)
    _archive_transcripts(remote_archive, codex_root)
    with Archive(remote_archive) as archive:
        conversations = archive.conversations()
    assert len(conversations) == 2

    destination = tmp_path / "claude-output"
    real_locked_sync = synchronization._sync_conversations_locked
    counter_lock = threading.Lock()
    active = maximum_active = 0

    def observed_locked_sync(*args: object, **kwargs: object) -> object:
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.05)
            return real_locked_sync(*args, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(synchronization, "_sync_conversations_locked", observed_locked_sync)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                synchronization.sync_conversations,
                [conversation],
                destination=destination,
                provider=get_provider("claude"),
                current_hostname="local-host",
            )
            for conversation in conversations
        ]
        for future in futures:
            future.result()

    assert maximum_active == 1
    manifest = json.loads((destination / MANIFEST_NAME).read_text())
    assert len(manifest["files"]) == 2
    assert all((destination / relative_path).is_file() for relative_path in manifest["files"])


def test_logical_session_identity_prevents_cross_provider_upload_feedback(
    tmp_path: Path,
    remote_archive: Path,
) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/2026/07/14/rollout-source.jsonl"
    _write_jsonl(codex_path, _codex_records())
    claude_root = tmp_path / ".claude"
    first = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(codex_root),
            "--dir",
            str(claude_root),
            "--provider",
            "codex",
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )
    assert first.exit_code == 0, first.output
    upload_copy = _archive_transcripts(remote_archive, claude_root, "claude")
    assert upload_copy.duplicates == 1

    cycle_arguments = [
        "sync",
        "--dir",
        str(codex_root),
        "--dir",
        str(claude_root),
        "--provider",
        "codex",
        "--provider",
        "claude",
        *_remote_options(),
    ]
    for _ in range(3):
        repeated_upload = _archive_transcripts(remote_archive, claude_root, "claude")
        assert repeated_upload.duplicates == 1
        repeated_sync = CliRunner().invoke(app, cycle_arguments)
        assert repeated_sync.exit_code == 0, repeated_sync.output
        assert len(_generated_paths(claude_root)) == 1

    with closing(sqlite3.connect(remote_archive)) as connection:
        metadata = [
            json.loads(row[0])
            for row in connection.execute("SELECT metadata_json FROM conversations")
        ]
    assert len(metadata) == 1
    identity = metadata[0]["_msync"]
    assert identity["chat_sha256"]
    assert identity["logical_session_id"] == "019f61a0-0000-7000-8000-000000000001"

    merged = tmp_path / "merged-claude"
    result = CliRunner().invoke(
        app,
        [
            "sync",
            "--dir",
            str(merged),
            "--provider",
            "claude",
            *_remote_options(),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(_generated_paths(merged)) == 1
    assert _generated_paths(merged)[0].relative_to(merged) == _generated_paths(claude_root)[
        0
    ].relative_to(claude_root)


def _generated_paths(root: Path) -> list[Path]:
    manifest = json.loads((root / MANIFEST_NAME).read_text())
    return sorted(root / relative_path for relative_path in manifest["files"])
