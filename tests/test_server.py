from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from msync.cli import app
from msync.database import Archive
from msync.providers import get_provider
from msync.server import ServerAccount, create_app, parse_server_accounts


def _archive_codex_conversation(
    database: Path,
    root: Path,
    *,
    session_id: str,
    message: str,
) -> None:
    transcript = root / "sessions" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "cwd": f"/work/{session_id}",
                            "git": {"branch": "feature/history"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:01Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": message},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:02Z",
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "Done."},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-14T12:00:03Z",
                        "type": "response_item",
                        "payload": {
                            "type": "reasoning",
                            "summary": [{"type": "summary_text", "text": "Private detail"}],
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    with Archive(database) as archive:
        archive.upload(root=root, provider=get_provider("codex"), transcripts=[transcript])


def _archive_claude_tool_conversation(database: Path, root: Path) -> None:
    transcript = root / "projects" / "-work" / "tool-session.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "user-1",
            "sessionId": "tool-session",
            "timestamp": "2026-07-14T12:00:00Z",
            "message": {"role": "user", "content": "Inspect the build log"},
        },
        {
            "type": "user",
            "uuid": "skill-context-1",
            "sessionId": "tool-session",
            "timestamp": "2026-07-14T12:00:00.500Z",
            "isMeta": True,
            "isSidechain": True,
            "message": {
                "role": "user",
                "content": (
                    "Base directory for this skill: /opt/skills/review"
                    "\n\nLarge internal instructions"
                ),
            },
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "sessionId": "tool-session",
            "timestamp": "2026-07-14T12:00:01Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will check it."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "/work/build.log"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "uuid": "tool-result-1",
            "sessionId": "tool-session",
            "timestamp": "2026-07-14T12:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "Build completed successfully",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "assistant-2",
            "sessionId": "tool-session",
            "timestamp": "2026-07-14T12:00:03Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "The build passed."}],
            },
        },
    ]
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
    with Archive(database) as archive:
        archive.upload(root=root, provider=get_provider("claude"), transcripts=[transcript])


def test_server_requires_basic_auth_for_ui_and_api(tmp_path: Path) -> None:
    web_app = create_app(tmp_path / "archive.sqlite", username="reader", password="secret")

    with TestClient(web_app) as client:
        page = client.get("/")
        api = client.get("/api/locations")
        wrong = client.get("/", auth=("reader", "wrong"))

    assert page.status_code == 401
    assert page.headers["www-authenticate"].startswith("Basic")
    assert api.status_code == 401
    assert wrong.status_code == 401


def test_server_account_configuration_accepts_optional_tokens() -> None:
    accounts = parse_server_accounts(
        "user1,password1,token1;user2,password2;user3,password3,"
    )

    assert accounts == (
        ServerAccount("user1", "password1", "token1"),
        ServerAccount("user2", "password2"),
        ServerAccount("user3", "password3"),
    )


def test_server_account_configuration_rejects_ambiguous_or_duplicate_credentials() -> None:
    invalid = (
        "user,name,password,token",
        "user,password,token;user,password2,token2",
        "user1,password1,token;user2,password2,token",
        "user-only",
    )

    for value in invalid:
        try:
            parse_server_accounts(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted invalid server accounts: {value}")


def test_remote_upload_tokens_isolate_accounts_and_optional_token_is_browser_only(
    tmp_path: Path,
) -> None:
    database = tmp_path / "archive.sqlite"
    accounts = (
        ServerAccount("alice", "alice-password", "alice-token"),
        ServerAccount("bob", "bob-password"),
        ServerAccount("carol", "carol-password", "carol-token"),
    )
    web_app = create_app(database, accounts=accounts)
    content = (
        json.dumps(
            {
                "timestamp": "2026-07-14T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": "shared-session", "cwd": "/tmp"},
            }
        )
        + "\n"
    ).encode()
    payload = {
        "version": 1,
        "provider": "codex",
        "hostname": "shared-hostname",
        "root_path": "/home/client/.codex",
        "display_name": ".codex",
        "transcripts": [
            {
                "relative_path": "sessions/shared.jsonl",
                "content_base64": base64.b64encode(content).decode(),
                "source_mtime_ns": 123,
            }
        ],
    }

    with TestClient(web_app) as client:
        no_token = client.post("/api/upload", json=payload)
        bob_upload = client.post(
            "/api/upload",
            json=payload,
            headers={"Authorization": "Bearer bob-token"},
        )
        alice_upload = client.post(
            "/api/upload",
            json=payload,
            headers={"Authorization": "Bearer alice-token"},
        )
        carol_upload = client.post(
            "/api/upload",
            json=payload,
            headers={"Authorization": "Bearer carol-token"},
        )
        repeated_alice_upload = client.post(
            "/api/upload",
            json=payload,
            headers={"Authorization": "Bearer alice-token"},
        )

        alice_locations = client.get("/api/locations", auth=("alice", "alice-password"))
        bob_locations = client.get("/api/locations", auth=("bob", "bob-password"))
        carol_locations = client.get("/api/locations", auth=("carol", "carol-password"))
        alice_conversations = client.get(
            "/api/conversations", auth=("alice", "alice-password")
        )
        carol_conversations = client.get(
            "/api/conversations", auth=("carol", "carol-password")
        )
        carol_conversation_id = carol_conversations.json()[0]["id"]
        cross_account_detail = client.get(
            f"/api/conversations/{carol_conversation_id}",
            auth=("alice", "alice-password"),
        )

    assert no_token.status_code == 401
    assert no_token.headers["www-authenticate"] == "Bearer"
    assert bob_upload.status_code == 401
    assert alice_upload.status_code == 200
    assert alice_upload.json()["imported"] == 1
    assert carol_upload.status_code == 200
    assert carol_upload.json()["imported"] == 1
    assert repeated_alice_upload.status_code == 200
    assert repeated_alice_upload.json()["unchanged"] == 1
    assert len(alice_locations.json()) == 1
    assert bob_locations.json() == []
    assert len(carol_locations.json()) == 1
    assert len(alice_conversations.json()) == 1
    assert len(carol_conversations.json()) == 1
    assert cross_account_detail.status_code == 404

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT account_username, count(*) FROM locations "
            "GROUP BY account_username ORDER BY account_username"
        ).fetchall() == [("alice", 1), ("carol", 1)]
        assert connection.execute(
            "SELECT account_username, count(*) FROM conversations "
            "GROUP BY account_username ORDER BY account_username"
        ).fetchall() == [("alice", 1), ("carol", 1)]


def test_remote_upload_rejects_paths_outside_the_source_root(tmp_path: Path) -> None:
    web_app = create_app(
        tmp_path / "archive.sqlite",
        accounts=(ServerAccount("alice", "password", "token"),),
    )
    payload = {
        "provider": "codex",
        "hostname": "laptop",
        "root_path": "/home/alice/.codex",
        "display_name": ".codex",
        "transcripts": [
            {
                "relative_path": "../escape.jsonl",
                "content_base64": base64.b64encode(b"{}\n").decode(),
            }
        ],
    }

    with TestClient(web_app) as client:
        response = client.post(
            "/api/upload",
            json=payload,
            headers={"Authorization": "Bearer token"},
        )

    assert response.status_code == 400
    assert "relative and contained" in response.json()["detail"]


def test_server_browses_and_filters_locations(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    first_root = tmp_path / ".codex-one"
    second_root = tmp_path / ".codex-two"
    _archive_codex_conversation(
        database,
        first_root,
        session_id="first-session",
        message="Investigate the blue widget",
    )
    _archive_codex_conversation(
        database,
        second_root,
        session_id="second-session",
        message="Ship the green gadget",
    )
    web_app = create_app(database, username="reader", password="secret")

    with TestClient(web_app) as client:
        client.auth = ("reader", "secret")
        locations = client.get("/api/locations")
        all_conversations = client.get("/api/conversations")
        search_results = client.get("/api/conversations", params={"search": "blue widget"})
        location_id = locations.json()[1]["id"]
        selected_location = client.get("/api/conversations", params={"location": location_id})
        first_page = client.get("/api/conversations", params={"limit": 1, "offset": 0})
        second_page = client.get("/api/conversations", params={"limit": 1, "offset": 1})
        oldest = client.get("/api/conversations", params={"order": "oldest"})
        by_title = client.get("/api/conversations", params={"order": "title"})
        invalid_order = client.get("/api/conversations", params={"order": "random"})

    assert locations.status_code == 200
    assert len(locations.json()) == 2
    assert all(location["hostname"] for location in locations.json())
    assert {location["conversation_count"] for location in locations.json()} == {1}
    assert len(all_conversations.json()) == 2
    assert all(conversation["hostname"] for conversation in all_conversations.json())
    assert [item["external_id"] for item in search_results.json()] == ["first-session"]
    assert len(selected_location.json()) == 1
    assert selected_location.json()[0]["location_id"] == location_id
    assert len(first_page.json()) == 1
    assert len(second_page.json()) == 1
    assert first_page.json()[0]["id"] != second_page.json()[0]["id"]
    assert [item["external_id"] for item in all_conversations.json()] == [
        "second-session",
        "first-session",
    ]
    assert [item["external_id"] for item in oldest.json()] == [
        "first-session",
        "second-session",
    ]
    assert [item["external_id"] for item in by_title.json()] == [
        "first-session",
        "second-session",
    ]
    assert invalid_order.status_code == 422


def test_server_returns_normalized_and_expandable_event_details(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    _archive_codex_conversation(
        database,
        tmp_path / ".codex",
        session_id="detail-session",
        message="Show all event detail",
    )
    web_app = create_app(database, username="reader", password="secret")

    with TestClient(web_app) as client:
        client.auth = ("reader", "secret")
        summary = client.get("/api/conversations").json()[0]
        response = client.get(f"/api/conversations/{summary['id']}")
        paged = client.get(
            f"/api/conversations/{summary['id']}",
            params={"event_limit": 2, "event_offset": 1},
        )
        missing = client.get("/api/conversations/999999")
        page = client.get("/")
        script = client.get("/assets/app.js")
        styles = client.get("/assets/styles.css")

    assert response.status_code == 200
    detail = response.json()
    assert detail["summary"]["external_id"] == "detail-session"
    assert detail["summary"]["hostname"]
    assert detail["relative_path"] == "sessions/detail-session.jsonl"
    assert detail["summary"]["event_count"] == 4
    assert detail["summary"]["message_count"] == 2
    assert detail["events"][1]["role"] == "user"
    assert detail["events"][1]["text"] == "Show all event detail"
    assert json.loads(detail["events"][1]["raw_json"])["type"] == "event_msg"
    assert any(event["visibility"] == "model" for event in detail["events"])
    assert paged.status_code == 200
    assert paged.json()["summary"]["event_count"] == 4
    assert [event["sequence"] for event in paged.json()["events"]] == [1, 2]
    assert missing.status_code == 404
    assert "Raw events" in page.text
    assert "ctrlKey" in script.text
    assert "moveEventFocus" in script.text
    assert "location.hostname" in script.text
    assert "SESSION_PAGE_SIZE" in script.text
    assert "EVENT_PAGE_SIZE" in script.text
    assert "loadMoreEvents" in script.text
    assert "ensureDetails" in script.text
    assert "disclosure.children.length === 1" in script.text
    assert "matchesTranscriptQuery" in script.text
    assert "requestId !== state.listRequest" in script.text
    assert "reloadArchive" in script.text
    assert "copyConversationLink" in script.text
    assert "setSidebar" in script.text
    assert "updateTitleOverflow" in script.text
    assert "setFitWidth" in script.text
    assert "moveHumanMessage" in script.text
    assert "renderMarkdownTable" in script.text
    assert 'data-transcript-filter="tools"' in page.text
    assert 'id="load-more"' in page.text
    assert 'id="copy-link"' in page.text
    assert 'id="transcript-search"' in page.text
    assert 'id="sidebar-scrim"' in page.text
    assert 'id="order-select"' in page.text
    assert 'id="toggle-width"' in page.text
    assert 'id="conversation-title-tooltip"' in page.text
    assert 'id="previous-human"' in page.text
    assert 'id="next-human"' in page.text
    assert ".session-list" in styles.text
    assert "overflow-y: auto" in styles.text
    assert "min-height: 0" in styles.text
    assert ".filter-count" in styles.text
    assert ".transcript-search" in styles.text
    assert ".transcript-load-more" in styles.text
    assert ".sidebar-scrim:not(.hidden)" in styles.text
    assert ".conversation.fit-width" in styles.text
    assert ".title-tooltip" in styles.text
    assert ".human-nav" in styles.text
    assert ".markdown-table" in styles.text
    assert page.headers["content-security-policy"].startswith("default-src 'self'")


def test_server_separates_claude_tool_activity_from_human_messages(tmp_path: Path) -> None:
    database = tmp_path / "archive.sqlite"
    _archive_claude_tool_conversation(database, tmp_path / ".claude")
    web_app = create_app(database, username="reader", password="secret")

    with TestClient(web_app) as client:
        client.auth = ("reader", "secret")
        summary = client.get("/api/conversations").json()[0]
        detail = client.get(f"/api/conversations/{summary['id']}").json()
        script = client.get("/assets/app.js").text
        styles = client.get("/assets/styles.css").text

    assert summary["title"] == "Inspect the build log"
    assert summary["preview"] == "Inspect the build log"
    assert summary["message_count"] == 3
    assert [event["role"] for event in detail["events"]] == [
        "user",
        "metadata",
        "assistant",
        "tool",
        "assistant",
    ]
    assert detail["events"][1]["event_subtype"] == "skill_context"
    assert detail["events"][1]["visibility"] == "metadata"
    assert detail["events"][1]["text"] == ""
    assert detail["events"][2]["text"] == "I will check it."
    assert detail["events"][3]["event_subtype"] == "tool_result"
    assert detail["events"][3]["text"] == "Build completed successfully"
    assert "conversationItems" in script
    assert "isInjectedClaudeContext" in script
    assert "Claude skill/context" in script
    assert "appendToolItem" in script
    assert "renderMarkdown" in script
    assert "hasEmbeddedOutput" in script
    assert "Completed without textual output" in script
    assert "innerHTML" not in script
    assert "navigator.clipboard" in script
    assert "tool-output-disclosure" in styles
    assert ".tool-finished" in styles
    assert ".context-notice" in styles


def test_server_command_starts_uvicorn(monkeypatch: Any, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(web_app: Any, *, host: str, port: int) -> None:
        captured.update(app=web_app, host=host, port=port)
        web_app.state.archive.close()

    monkeypatch.setattr("uvicorn.run", fake_run)
    database = tmp_path / "server.sqlite"

    result = CliRunner().invoke(
        app,
        [
            "server",
            "--password",
            "secret",
            "--username",
            "reader",
            "--database",
            str(database),
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["app"].title == "msync history browser"
    assert "http://127.0.0.1:8765" in result.output


def test_server_command_accepts_multi_account_configuration(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(web_app: Any, *, host: str, port: int) -> None:
        captured.update(app=web_app, host=host, port=port)
        web_app.state.archive.close()

    monkeypatch.setattr("uvicorn.run", fake_run)
    result = CliRunner().invoke(
        app,
        [
            "server",
            "--accounts",
            "alice,alice-password,alice-token;bob,bob-password",
            "--database",
            str(tmp_path / "server.sqlite"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "one of 2 configured accounts" in result.output
    assert captured["app"].title == "msync history browser"


def test_server_command_leaves_old_schema_unchanged_when_upgrade_declined(
    tmp_path: Path,
) -> None:
    database = tmp_path / "server.sqlite"
    with Archive(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")

    result = CliRunner().invoke(
        app,
        ["server", "--password", "secret", "--database", str(database)],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Upgrade the database now? [y/N]" in result.output
    assert "database was not upgraded" in result.output
    assert "Server failed" not in result.output
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone() == ("5",)


def test_server_command_upgrades_old_schema_when_confirmed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    database = tmp_path / "server.sqlite"
    with Archive(database):
        pass
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_info SET value = '5' WHERE key = 'schema_version'")
        connection.execute("PRAGMA user_version = 5")

    captured: dict[str, Any] = {}

    def fake_run(web_app: Any, *, host: str, port: int) -> None:
        captured.update(app=web_app, host=host, port=port)
        web_app.state.archive.close()

    monkeypatch.setattr("uvicorn.run", fake_run)
    result = CliRunner().invoke(
        app,
        ["server", "--password", "secret", "--database", str(database)],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert "Upgrade the database now? [y/N]" in result.output
    assert "Database schema upgrade complete: 5 → 7" in result.output
    assert captured["app"].title == "msync history browser"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key = 'schema_version'"
        ).fetchone() == ("7",)


def test_server_rejects_empty_credentials(tmp_path: Path) -> None:
    try:
        create_app(tmp_path / "archive.sqlite", username="", password="secret")
    except ValueError as error:
        assert str(error) == "Server username must not be empty."
    else:
        raise AssertionError("create_app accepted an empty username")
