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


@pytest.mark.parametrize(
    ("provider_name", "relative_path", "records"),
    [
        (
            "claude",
            "projects/-work/legacy.jsonl",
            [
                {
                    "type": "user",
                    "uuid": "message-1",
                    "sessionId": "converted-session",
                    "message": {"role": "user", "content": "legacy copy"},
                    "msync": {
                        "sourceProvider": "codex",
                        "sourceConversationId": "019f61a0-0000-7000-8000-000000000055",
                        "sourceKey": "legacy-source",
                    },
                }
            ],
        ),
        (
            "codex",
            "sessions/2026/07/14/legacy.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "converted-session",
                        "msync": {
                            "source_provider": "claude",
                            "source_conversation_id": "019f61a0-0000-7000-8000-000000000055",
                            "source_key": "legacy-source",
                        },
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "legacy copy"},
                },
            ],
        ),
    ],
)
def test_legacy_generated_provenance_recovers_the_source_session_identity(
    tmp_path: Path,
    provider_name: str,
    relative_path: str,
    records: list[dict[str, Any]],
) -> None:
    root = tmp_path / provider_name
    path = root / relative_path
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))

    conversation = get_provider(provider_name).read(path, root)

    assert conversation.logical_session_id == "019f61a0-0000-7000-8000-000000000055"


def test_registry_rejects_duplicates_and_unknown_names() -> None:
    registry = ProviderRegistry((ExampleProvider(),))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(ExampleProvider())
    with pytest.raises(HistoryFormatError, match="Available providers: example"):
        registry.get("missing")


def test_claude_tool_blocks_are_not_normalized_as_user_messages() -> None:
    provider = get_provider("claude")
    tool_result = {
        "type": "user",
        "uuid": "tool-result-1",
        "sessionId": "session-1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-use-1",
                    "content": "command output",
                }
            ],
        },
    }

    event = provider.decode_event(0, json.dumps(tool_result), tool_result)

    assert event.role == "tool"
    assert event.event_subtype == "tool_result"
    assert event.visibility == "display"
    assert event.searchable_text == "command output"
    assert [part.content_type for part in event.parts] == ["tool_result"]


def test_claude_mixed_message_keeps_prose_separate_from_tool_payload() -> None:
    provider = get_provider("claude")
    mixed_message = {
        "type": "assistant",
        "uuid": "assistant-1",
        "sessionId": "session-1",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will inspect the file."},
                {
                    "type": "tool_use",
                    "id": "tool-use-1",
                    "name": "Read",
                    "input": {"file_path": "/work/example.py"},
                },
            ],
        },
    }

    event = provider.decode_event(0, json.dumps(mixed_message), mixed_message)

    assert event.role == "assistant"
    assert event.searchable_text == "I will inspect the file."
    assert [part.content_type for part in event.parts] == ["text", "tool_use"]
    assert json.loads(event.parts[1].raw_json)["name"] == "Read"


@pytest.mark.parametrize("subtype", ["function_call", "function_call_output", "mcp_call"])
def test_codex_tool_items_are_separate_display_events(subtype: str) -> None:
    provider = get_provider("codex")
    payload = {
        "type": subtype,
        "call_id": "call-1",
        "name": "exec_command",
        "arguments": '{"cmd":"pwd"}',
        "output": "finished",
    }
    value = {"type": "response_item", "payload": payload}

    event = provider.decode_event(0, json.dumps(value), value)

    assert event.role == "tool"
    assert event.event_subtype == subtype
    assert event.visibility == "display"
    assert event.parts[0].content_type == subtype
