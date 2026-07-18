from __future__ import annotations

import importlib.util
import io
from argparse import Namespace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_skill_script() -> ModuleType:
    path = (
        Path(__file__).parents[1] / "plugins/msync/skills/recall-history/scripts/remote_history.py"
    )
    spec = importlib.util.spec_from_file_location("msync_recall_history", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recall_history_sends_bearer_token_without_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_skill_script()
    captured: dict[str, Any] = {}

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            del args

    class Opener:
        def open(self, request: Any, *, timeout: int) -> Response:
            captured["request"] = request
            captured["timeout"] = timeout
            return Response(b'[{"id": 42}]')

    def fake_build_opener(*handlers: Any) -> Opener:
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(module, "build_opener", fake_build_opener)
    payload = module._request_json(
        "https://history.example",
        "secret-token",
        "/api/conversations",
        {"search": "migration"},
    )

    request = captured["request"]
    assert payload == [{"id": 42}]
    assert request.full_url == "https://history.example/api/conversations?search=migration"
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert captured["timeout"] == module.REQUEST_TIMEOUT_SECONDS
    assert isinstance(captured["handlers"][0], module._RejectRedirects)


def test_recall_history_filters_search_to_current_project(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_skill_script()
    requests: list[dict[str, Any]] = []

    def fake_request(
        base_url: str,
        token: str,
        path: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del base_url, token, path
        requests.append(params)
        return [
            {
                "id": 42,
                "provider": "codex",
                "external_id": "matching",
                "cwd": "/work/msync",
                "preview": "Discuss the migration",
            },
            {
                "id": 99,
                "provider": "claude",
                "external_id": "other",
                "cwd": "/work/another-project",
            },
        ]

    monkeypatch.setattr(module, "_request_json", fake_request)
    monkeypatch.setattr(module, "_project_aliases", lambda: {"msync"})
    module._search(
        Namespace(query="migration", project=True, limit=10, offset=0, order="newest"),
        "https://history.example",
        "secret-token",
    )

    output = capsys.readouterr().out
    assert "42 | codex" in output
    assert "99 | claude" not in output
    assert requests[0]["limit"] == 500
    assert requests[0]["offset"] == 0
    assert requests[0]["preview_chars"] == 300


def test_recall_history_pages_until_it_finds_project_matches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_skill_script()
    offsets: list[int] = []

    def fake_request(
        base_url: str,
        token: str,
        path: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del base_url, token, path
        offsets.append(params["offset"])
        if params["offset"] == 0:
            return [
                {"id": 1, "cwd": "/work/msync"},
                *[
                    {"id": identifier, "cwd": "/work/another-project"}
                    for identifier in range(2, 501)
                ],
            ]
        return [
            {
                "id": 501,
                "provider": "codex",
                "external_id": "matching",
                "cwd": "/work/msync",
            }
        ]

    monkeypatch.setattr(module, "_request_json", fake_request)
    monkeypatch.setattr(module, "_project_aliases", lambda: {"msync"})
    module._search(
        Namespace(query="migration", project=True, limit=1, offset=1, order="newest"),
        "https://history.example",
        "secret-token",
    )

    output = capsys.readouterr().out
    assert offsets == [0, 500]
    assert "501 | codex" in output


def test_recall_history_reads_visible_messages_and_reports_next_page(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_skill_script()
    captured: dict[str, Any] = {}

    def fake_request(
        base_url: str,
        token: str,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        del base_url, token
        captured["path"] = path
        captured["params"] = params
        return {
            "summary": {
                "id": 42,
                "external_id": "session-42",
                "provider": "codex",
                "cwd": "/work/msync",
                "event_count": 8,
            },
            "events": [
                {"sequence": 0, "event_type": "metadata", "role": None, "text": "hidden"},
                {"sequence": 1, "event_type": "message", "role": "user", "text": "Question"},
                {
                    "sequence": 2,
                    "event_type": "message",
                    "role": "assistant",
                    "text": "Answer",
                },
            ],
        }

    monkeypatch.setattr(module, "_request_json", fake_request)
    module._read(
        Namespace(conversation_id=42, limit=3, offset=0, max_chars=12_000, all_events=False),
        "https://history.example",
        "secret-token",
    )

    output = capsys.readouterr().out
    assert "[1 | user]" in output
    assert "[2 | assistant]" in output
    assert "hidden" not in output
    assert "continue with --offset 3" in output
    assert captured["path"] == "/api/conversations/42/context"
    assert captured["params"] == {
        "event_limit": 3,
        "event_offset": 0,
        "max_chars": 12_000,
    }


def test_recall_history_prints_server_bounded_event_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_skill_script()
    monkeypatch.setattr(
        module,
        "_request_json",
        lambda *args, **kwargs: {
            "summary": {
                "id": 42,
                "external_id": "session-42",
                "provider": "codex",
                "event_count": 1,
            },
            "events": [
                {
                    "sequence": 0,
                    "event_type": "message",
                    "role": "assistant",
                    "text": "abcd\n[… 6 characters omitted]",
                }
            ],
        },
    )
    module._read(
        Namespace(conversation_id=42, limit=1, offset=0, max_chars=4, all_events=False),
        "https://history.example",
        "secret-token",
    )

    output = capsys.readouterr().out
    assert "abcd\n[… 6 characters omitted" in output
    assert "efghij" not in output
