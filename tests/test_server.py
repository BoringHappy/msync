from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from msync.cli import app
from msync.database import Archive
from msync.providers import get_provider
from msync.server import create_app


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

    assert locations.status_code == 200
    assert len(locations.json()) == 2
    assert {location["conversation_count"] for location in locations.json()} == {1}
    assert len(all_conversations.json()) == 2
    assert [item["external_id"] for item in search_results.json()] == ["first-session"]
    assert len(selected_location.json()) == 1
    assert selected_location.json()[0]["location_id"] == location_id


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
        missing = client.get("/api/conversations/999999")
        page = client.get("/")
        script = client.get("/assets/app.js")

    assert response.status_code == 200
    detail = response.json()
    assert detail["summary"]["external_id"] == "detail-session"
    assert detail["relative_path"] == "sessions/detail-session.jsonl"
    assert detail["summary"]["event_count"] == 4
    assert detail["summary"]["message_count"] == 2
    assert detail["events"][1]["role"] == "user"
    assert detail["events"][1]["text"] == "Show all event detail"
    assert json.loads(detail["events"][1]["raw_json"])["type"] == "event_msg"
    assert any(event["visibility"] == "model" for event in detail["events"])
    assert missing.status_code == 404
    assert "Expand details" in page.text
    assert "ctrlKey" in script.text
    assert page.headers["content-security-policy"].startswith("default-src 'self'")


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


def test_server_rejects_empty_credentials(tmp_path: Path) -> None:
    try:
        create_app(tmp_path / "archive.sqlite", username="", password="secret")
    except ValueError as error:
        assert str(error) == "Server username must not be empty."
    else:
        raise AssertionError("create_app accepted an empty username")
