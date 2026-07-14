from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from msync.models import Event
from msync.providers import ProviderRegistry, get_provider, provider_names
from msync.providers.base import (
    ConversationDetails,
    HistoryFormatError,
    HistoryProvider,
    message_parts,
)


class ExampleProvider(HistoryProvider):
    """Minimal third provider proving the extension contract."""

    name = "example"
    search_directories = ("threads",)

    def matches_record(self, record: dict[str, Any]) -> bool:
        return record.get("provider") == self.name

    def decode_event(self, sequence: int, raw_json: str, value: dict[str, Any]) -> Event:
        return Event(
            sequence=sequence,
            raw_json=raw_json,
            event_type="message",
            role="user",
            visibility="display",
            parts=message_parts(value.get("message")),
        )

    def conversation_details(
        self, events: tuple[Event, ...], path: Path, relative_path: str
    ) -> ConversationDetails:
        del events, relative_path
        return ConversationDetails(external_id=path.stem)


def test_claude_and_codex_are_parallel_registered_providers() -> None:
    assert provider_names() == ("claude", "codex")
    assert get_provider("claude").name == "claude"
    assert get_provider("codex").name == "codex"


@pytest.mark.parametrize(
    ("directory_name", "other_layout", "expected"),
    [
        ("backup-CLAUDE-alt", "sessions", "claude"),
        ("team-codex-2", "projects", "codex"),
    ],
)
def test_provider_name_containment_is_checked_before_internal_layout(
    tmp_path: Path, directory_name: str, other_layout: str, expected: str
) -> None:
    root = tmp_path / directory_name
    (root / other_layout).mkdir(parents=True)

    registry = ProviderRegistry((get_provider("claude"), get_provider("codex")))

    assert registry.detect(root).name == expected


@pytest.mark.parametrize(
    ("subdirectory", "record", "expected"),
    [
        (
            "projects/work",
            {"type": "user", "sessionId": "claude-session", "uuid": "message-1"},
            "claude",
        ),
        (
            "sessions/2026/07/14",
            {"type": "session_meta", "payload": {"id": "codex-session"}},
            "codex",
        ),
    ],
)
def test_provider_is_detected_from_fixed_internal_files(
    tmp_path: Path, subdirectory: str, record: dict[str, Any], expected: str
) -> None:
    root = tmp_path / "neutral-history"
    path = root / subdirectory / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record) + "\n")
    registry = ProviderRegistry((get_provider("claude"), get_provider("codex")))

    assert registry.detect(root).name == expected


def test_new_provider_can_be_added_through_the_shared_contract(tmp_path: Path) -> None:
    root = tmp_path / ".example"
    path = root / "threads/example-session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"provider": "example", "message": "hello extension"}) + "\n")
    adapter = ExampleProvider()
    registry = ProviderRegistry((adapter,))

    assert registry.detect(root) is adapter
    assert adapter.discover(root) == [path.resolve()]
    conversation = adapter.read(path.resolve(), root.resolve())
    assert conversation.provider == "example"
    assert conversation.external_id == "example-session"
    assert conversation.title == "hello extension"


def test_registry_rejects_duplicates_and_unknown_names() -> None:
    registry = ProviderRegistry((ExampleProvider(),))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(ExampleProvider())
    with pytest.raises(HistoryFormatError, match="Available providers: example"):
        registry.get("missing")
