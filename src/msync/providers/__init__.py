"""Provider registry and built-in Claude/Codex adapters."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from msync.providers.base import HistoryFormatError, HistoryProvider, first_json_record
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
        name_matches = [
            provider for provider in self._providers.values() if provider.matches_name(root)
        ]
        if len(name_matches) == 1:
            return name_matches[0]
        if name_matches:
            names = ", ".join(provider.name for provider in name_matches)
            raise HistoryFormatError(f"Ambiguous history directory {root}; matched: {names}.")

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
            return content_matches[0]
        if content_matches:
            names = ", ".join(provider.name for provider in content_matches)
            raise HistoryFormatError(f"Ambiguous history content in {root}; matched: {names}.")

        choices = ", ".join(self.names)
        raise HistoryFormatError(
            f"Could not detect history in {root}. Use --provider with one of: {choices}."
        )


registry = ProviderRegistry((ClaudeProvider(), CodexProvider()))


def get_provider(name: str) -> HistoryProvider:
    return registry.get(name)


def detect_provider(root: Path) -> HistoryProvider:
    return registry.detect(root)


def provider_names() -> tuple[str, ...]:
    return registry.names


__all__ = [
    "HistoryFormatError",
    "HistoryProvider",
    "ProviderRegistry",
    "detect_provider",
    "get_provider",
    "provider_names",
    "registry",
]
