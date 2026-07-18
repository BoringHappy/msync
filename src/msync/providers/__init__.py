"""Provider registry and built-in Claude/Codex adapters."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from msync.providers.base import (
    HistoryFormatError,
    HistoryProvider,
    NoTransferableMessagesError,
    first_json_record,
)
from msync.providers.claude import ClaudeProvider
from msync.providers.codex import CodexProvider


class ProviderRegistry:
    """Ordered registry used for provider lookup and automatic detection."""

    def __init__(self, providers: Iterable[HistoryProvider] = ()) -> None:
        self._providers: dict[str, HistoryProvider] = {}
        for provider in providers:
            self.register(provider)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def register(self, provider: HistoryProvider) -> None:
        if not provider.name or provider.name == "auto" or len(provider.name) > 64:
            raise ValueError("Provider names must be 1-64 characters and cannot be 'auto'.")
        if provider.name in self._providers:
            raise ValueError(f"Provider {provider.name!r} is already registered.")
        self._providers[provider.name] = provider

    def get(self, name: str) -> HistoryProvider:
        try:
            return self._providers[name]
        except KeyError as error:
            choices = ", ".join(self.names)
            raise HistoryFormatError(
                f"Unknown history provider {name!r}. Available providers: {choices}."
            ) from error

    def detect(self, root: Path) -> HistoryProvider:
        provider = self.detect_optional(root)
        if provider is not None:
            return provider
        choices = ", ".join(self.names)
        raise HistoryFormatError(
            f"Could not detect history in {root}. Use --provider with one of: {choices}."
        )

    def detect_optional(self, root: Path) -> HistoryProvider | None:
        """Detect existing provider identity, returning None for a neutral empty location."""

        name_matches = [
            provider for provider in self._providers.values() if provider.matches_name(root)
        ]
        if len(name_matches) > 1:
            names = ", ".join(provider.name for provider in name_matches)
            raise HistoryFormatError(f"Ambiguous history directory {root}; matched: {names}.")
        name_match = name_matches[0] if name_matches else None

        records: dict[Path, dict[str, object] | None] = {}
        content_matches: list[HistoryProvider] = []
        for provider in self._providers.values():
            for path in provider.detection_paths(root):
                if path not in records:
                    records[path] = first_json_record(path)
                record = records[path]
                if record is not None and provider.matches_record(record):
                    content_matches.append(provider)
                    break
        if len(content_matches) == 1:
            content_match = content_matches[0]
            if name_match is not None and name_match is not content_match:
                raise HistoryFormatError(
                    f"History directory name suggests {name_match.name}, but its content "
                    f"matches {content_match.name}: {root}."
                )
            return content_match
        if content_matches:
            names = ", ".join(provider.name for provider in content_matches)
            raise HistoryFormatError(f"Ambiguous history content in {root}; matched: {names}.")

        return name_match


registry = ProviderRegistry((ClaudeProvider(), CodexProvider()))


def get_provider(name: str) -> HistoryProvider:
    return registry.get(name)


def detect_provider(root: Path) -> HistoryProvider:
    return registry.detect(root)


def detect_existing_provider(root: Path) -> HistoryProvider | None:
    return registry.detect_optional(root)


def provider_names() -> tuple[str, ...]:
    return registry.names


__all__ = [
    "HistoryFormatError",
    "HistoryProvider",
    "NoTransferableMessagesError",
    "ProviderRegistry",
    "detect_existing_provider",
    "detect_provider",
    "get_provider",
    "provider_names",
    "registry",
]
