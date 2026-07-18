from __future__ import annotations

import json
import sqlite3
import zlib
from contextlib import closing
from pathlib import Path

from msync.database import Archive
from msync.providers import detect_provider, get_provider


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    path.write_text(content)
    return content.encode()


def _codex_records(session_id: str = "session-1") -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-07-14T10:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/work/project",
                "cli_version": "1.2.3",
                "model_provider": "openai",
                "git": {"branch": "main", "commit_hash": "abc123"},
            },
        },
        {
            "timestamp": "2026-07-14T10:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-test"},
        },
        {
            "timestamp": "2026-07-14T10:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Find the blue widget"},
        },
        {
            "timestamp": "2026-07-14T10:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I found it."}],
            },
        },
    ]


def test_codex_upload_is_lossless_searchable_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript_path = root / "sessions/2026/07/14/rollout-session-1.jsonl"
    original = _write_jsonl(transcript_path, _codex_records())
    database = tmp_path / "archive.sqlite"

    provider = detect_provider(root)
    paths = provider.discover(root)
    with Archive(database) as archive:
        assert archive.archive_revision() == 0
        first = archive.upload(root=root, provider=provider, transcripts=paths)
        first_metrics = archive.browse_metrics()
        cached_metrics = archive.browse_metrics()
        second = archive.upload(root=root, provider=provider, transcripts=paths)
        unchanged_revision = archive.archive_revision()

    assert provider.name == "codex"
    assert first.imported == 1
    assert first.events == 4
    assert second.imported == 0
    assert second.unchanged == 1
    assert first_metrics.revision == 1
    assert cached_metrics is first_metrics
    assert unchanged_revision == 1

    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """
            SELECT external_id, title, cwd, model, git_branch, source_size, transcript
            FROM conversations
            """
        ).fetchone()
        assert row is not None
        assert row[:6] == (
            "session-1",
            "Find the blue widget",
            "/work/project",
            "gpt-test",
            "main",
            len(original),
        )
        assert zlib.decompress(row[6]) == original
        match = connection.execute(
            "SELECT searchable_text FROM events_fts WHERE events_fts MATCH 'widget'"
        ).fetchone()
        assert match == ("Find the blue widget",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT event_count, message_count, preview FROM conversation_metrics"
        ).fetchone() == (4, 2, "Find the blue widget")
        assert connection.execute(
            "SELECT account_username, revision FROM archive_revisions"
        ).fetchone() == ("", 1)


def test_overview_aggregates_native_claude_and_codex_token_usage(tmp_path: Path) -> None:
    codex_root = tmp_path / ".codex"
    codex_path = codex_root / "sessions/codex.jsonl"
    codex_records = _codex_records("codex-tokens")
    codex_records.append(
        {
            "timestamp": "2026-07-14T10:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    }
                },
            },
        }
    )
    _write_jsonl(codex_path, codex_records)

    claude_root = tmp_path / ".claude"
    claude_path = claude_root / "projects/-work/claude.jsonl"
    _write_jsonl(
        claude_path,
        [
            {
                "type": "user",
                "uuid": "claude-user",
                "sessionId": "claude-tokens",
                "message": {"role": "user", "content": "Count these tokens"},
            },
            {
                "type": "assistant",
                "uuid": "claude-assistant",
                "sessionId": "claude-tokens",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Counted"}],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_creation_input_tokens": 3,
                        "cache_read_input_tokens": 2,
                    },
                },
            },
        ],
    )

    with Archive(tmp_path / "tokens.sqlite") as archive:
        for root, path, provider_name in (
            (codex_root, codex_path, "codex"),
            (claude_root, claude_path, "claude"),
        ):
            archive.upload(
                root=root,
                provider=get_provider(provider_name),
                transcripts=[path],
            )
        totals = archive.browse_metrics().totals

    assert totals.input_tokens == 115
    assert totals.output_tokens == 25
    assert totals.cached_input_tokens == 45
    assert totals.tokens == 140


def test_normalized_text_replaces_database_incompatible_nul_bytes(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    transcript_path = root / "sessions/nul.jsonl"
    records = _codex_records("nul-session")
    records[2]["payload"] = {
        "type": "user_message",
        "message": "Before\x00after",
    }
    original = _write_jsonl(transcript_path, records)
    database = tmp_path / "archive.sqlite"

    with Archive(database) as archive:
        archive.upload(
            root=root,
            provider=get_provider("codex"),
            transcripts=[transcript_path],
        )

    with closing(sqlite3.connect(database)) as connection:
        title, transcript = connection.execute(
            "SELECT title, transcript FROM conversations"
        ).fetchone()
        event_text, raw_json = connection.execute(
            "SELECT searchable_text, raw_json FROM events WHERE role = 'user'"
        ).fetchone()
        part_text = connection.execute(
            """
            SELECT message_parts.text
            FROM message_parts JOIN events ON events.id = message_parts.event_id
            WHERE events.role = 'user'
            """
        ).fetchone()[0]

    assert title == "Before\N{REPLACEMENT CHARACTER}after"
    assert event_text == "Before\N{REPLACEMENT CHARACTER}after"
    assert part_text == "Before\N{REPLACEMENT CHARACTER}after"
    assert "\\u0000" in raw_json
    assert zlib.decompress(transcript) == original


def test_changed_transcript_replaces_normalized_events(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    path = root / "sessions/rollout.jsonl"
    _write_jsonl(path, _codex_records())
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")
    with Archive(database) as archive:
        archive.upload(root=root, provider=provider, transcripts=[path])
        previous_metrics = archive.browse_metrics()
        records = _codex_records()
        records[-1]["payload"] = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Updated answer"}],
        }
        _write_jsonl(path, records)
        result = archive.upload(root=root, provider=provider, transcripts=[path])
        updated_metrics = archive.browse_metrics()

    assert result.updated == 1
    assert updated_metrics.revision == previous_metrics.revision + 1
    assert updated_metrics is not previous_metrics
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (4,)
        assert connection.execute(
            "SELECT searchable_text FROM events_fts WHERE events_fts MATCH 'updated'"
        ).fetchone() == ("Updated answer",)


def test_metrics_cache_observes_revisions_from_another_archive_instance(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".codex"
    path = root / "sessions/rollout.jsonl"
    _write_jsonl(path, _codex_records())
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")
    with Archive(database) as writer:
        writer.upload(root=root, provider=provider, transcripts=[path])

    with Archive(database) as reader, Archive(database) as writer:
        initial = reader.browse_metrics()
        records = _codex_records()
        records.append(
            {
                "timestamp": "2026-07-14T10:00:04Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "One more update."},
            }
        )
        _write_jsonl(path, records)
        writer.upload(root=root, provider=provider, transcripts=[path])
        refreshed = reader.browse_metrics()

    assert refreshed.revision == initial.revision + 1
    assert refreshed.totals.events == initial.totals.events + 1
    assert refreshed.totals.messages == initial.totals.messages + 1


def test_same_logical_revision_from_two_locations_is_deduplicated(tmp_path: Path) -> None:
    first_root = tmp_path / ".codex"
    second_root = tmp_path / ".codex_another"
    first_path = first_root / "sessions/one.jsonl"
    second_path = second_root / "sessions/two.jsonl"
    _write_jsonl(first_path, _codex_records("shared-id"))
    _write_jsonl(second_path, _codex_records("shared-id"))
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")
    with Archive(database) as archive:
        first = archive.upload(root=first_root, provider=provider, transcripts=[first_path])
        second = archive.upload(root=second_root, provider=provider, transcripts=[second_path])

    assert first.imported == 1
    assert second.duplicates == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM locations").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (1,)


def test_same_path_on_different_hosts_creates_distinct_locations(tmp_path: Path) -> None:
    root = tmp_path / ".codex"
    path = root / "sessions/session.jsonl"
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")
    _write_jsonl(path, _codex_records("host-a-session"))
    with Archive(database, hostname="workstation-a") as archive:
        archive.upload(root=root, provider=provider, transcripts=[path])

    _write_jsonl(path, _codex_records("host-b-session"))
    with Archive(database, hostname="workstation-b") as archive:
        archive.upload(root=root, provider=provider, transcripts=[path])

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT hostname, root_path FROM locations ORDER BY hostname"
        ).fetchall() == [
            ("workstation-a", str(root.resolve())),
            ("workstation-b", str(root.resolve())),
        ]
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (2,)


def test_changed_revision_with_same_session_id_stays_separate(tmp_path: Path) -> None:
    first_root = tmp_path / ".codex"
    second_root = tmp_path / ".codex_another"
    first_path = first_root / "sessions/one.jsonl"
    second_path = second_root / "sessions/two.jsonl"
    _write_jsonl(first_path, _codex_records("shared-id"))
    changed = _codex_records("shared-id")
    changed[-1]["payload"] = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "A different revision"}],
    }
    _write_jsonl(second_path, changed)
    database = tmp_path / "archive.sqlite"
    provider = get_provider("codex")

    with Archive(database) as archive:
        archive.upload(root=first_root, provider=provider, transcripts=[first_path])
        result = archive.upload(root=second_root, provider=provider, transcripts=[second_path])

    assert result.imported == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (2,)


def test_claude_transcript_and_subagent_metadata(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    path = root / "projects/-work/session-parent/subagents/agent-child.jsonl"
    _write_jsonl(
        path,
        [
            {
                "type": "user",
                "uuid": "message-1",
                "sessionId": "session-child",
                "parentUuid": None,
                "timestamp": "2026-07-14T11:00:00Z",
                "cwd": "/work",
                "gitBranch": "feature",
                "version": "2.0.0",
                "message": {"role": "user", "content": "Explain frobnication"},
            },
            {
                "type": "assistant",
                "uuid": "message-2",
                "sessionId": "session-child",
                "parentUuid": "message-1",
                "timestamp": "2026-07-14T11:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "Certainly."}],
                },
            },
        ],
    )
    database = tmp_path / "archive.sqlite"
    provider = detect_provider(root)

    with Archive(database) as archive:
        result = archive.upload(
            root=root,
            provider=provider,
            transcripts=provider.discover(root),
        )

    assert result.imported == 1
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """
            SELECT external_id, conversation_kind, parent_external_id, model
            FROM conversations
            """
        ).fetchone()
        assert row == ("session-child", "subagent", "session-parent", "claude-test")
