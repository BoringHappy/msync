"""Safe, idempotent synchronization into native provider history directories."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from msync.database import ArchivedConversation
from msync.providers import HistoryFormatError, HistoryProvider

MANIFEST_NAME = ".msync-manifest.json"
MANIFEST_VERSION = 1


@dataclass(slots=True, frozen=True)
class SyncResult:
    """File outcomes for one native destination."""

    current: int = 0
    written: int = 0
    unchanged: int = 0
    protected: int = 0
    skipped: int = 0
    equivalent: int = 0
    conflicts: tuple[str, ...] = ()


def unmanaged_transcripts(root: Path, transcripts: list[Path]) -> list[Path]:
    """Exclude byte-unchanged files previously written by msync from re-import."""

    manifest = _load_manifest(root)
    files = manifest["files"]
    unmanaged: list[Path] = []
    for path in transcripts:
        relative_path = path.relative_to(root).as_posix()
        entry = files.get(relative_path)
        if not isinstance(entry, dict) or entry.get("sha256") != _file_hash(path):
            unmanaged.append(path)
    return unmanaged


def sync_conversations(
    conversations: list[ArchivedConversation],
    *,
    destination: Path,
    provider: HistoryProvider,
) -> SyncResult:
    """Write all archived histories into one provider-native destination."""

    destination = destination.expanduser().resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = _load_manifest(destination)
    files: dict[str, dict[str, Any]] = manifest["files"]
    existing_revisions = _manifest_revision_paths(destination, provider, files)
    selected, current, equivalent = _collapse_equivalent_chats(
        conversations,
        destination=destination,
        provider=provider,
    )
    written = unchanged = protected = skipped = 0
    conflicts: list[str] = []

    for archived in selected:
        conversation = archived.conversation
        source_root = Path(archived.source_root).expanduser().resolve()
        if source_root == destination and conversation.provider == provider.name:
            current += 1
            continue

        source_key = _source_key(archived)
        revision_hash = conversation.chat_sha256 or conversation.sha256
        revision_identity = (conversation.logical_session_id, conversation.chat_sha256)
        legacy_path = existing_revisions.get(revision_identity)
        if conversation.chat_sha256 is not None and legacy_path is not None:
            entry = files[legacy_path]
            sources = set(entry.get("sources", []))
            sources.add(source_key)
            entry["sources"] = sorted(sources)
            unchanged += 1
            continue
        session_id = str(uuid5(UUID(conversation.logical_session_id), revision_hash))
        started_at = _started_at(archived)
        if conversation.provider == provider.name:
            relative_path = Path(conversation.relative_path)
            content = conversation.transcript
            _, native_path = _destination_path(destination, relative_path)
            native_hash = hashlib.sha256(content).hexdigest()
            if native_path.exists() and (
                not native_path.is_file() or _file_hash(native_path) != native_hash
            ):
                try:
                    content = provider.encode_conversation(
                        conversation,
                        session_id=session_id,
                        started_at=started_at,
                        source_key=source_key,
                    )
                except ValueError:
                    skipped += 1
                    continue
                relative_path = provider.export_relative_path(
                    conversation,
                    session_id=session_id,
                    started_at=started_at,
                )
        else:
            try:
                content = provider.encode_conversation(
                    conversation,
                    session_id=session_id,
                    started_at=started_at,
                    source_key=source_key,
                )
            except ValueError:
                skipped += 1
                continue
            relative_path = provider.export_relative_path(
                conversation,
                session_id=session_id,
                started_at=started_at,
            )

        relative_key, output_path = _destination_path(destination, relative_path)
        expected_hash = hashlib.sha256(content).hexdigest()
        entry = files.get(relative_key)
        if not isinstance(entry, dict):
            entry = None
        sources = set(entry.get("sources", [])) if entry else set()

        if output_path.exists():
            actual_hash = _file_hash(output_path)
            if actual_hash == expected_hash:
                unchanged += 1
            elif source_key in sources:
                protected += 1
                continue
            else:
                conflicts.append(relative_key)
                continue
        else:
            _atomic_write(output_path, content, destination=destination)
            written += 1

        sources.add(source_key)
        files[relative_key] = {"sha256": expected_hash, "sources": sorted(sources)}

    _write_manifest(destination, manifest)
    return SyncResult(
        current=current,
        written=written,
        unchanged=unchanged,
        protected=protected,
        skipped=skipped,
        equivalent=equivalent,
        conflicts=tuple(conflicts),
    )


def _manifest_revision_paths(
    destination: Path,
    provider: HistoryProvider,
    files: dict[str, dict[str, Any]],
) -> dict[tuple[str, str | None], str]:
    """Index managed histories by logical revision, including older msync path schemes."""

    revisions: dict[tuple[str, str | None], str] = {}
    for relative_path, entry in files.items():
        if not isinstance(entry, dict):
            continue
        try:
            relative_key, path = _destination_path(destination, Path(relative_path))
        except HistoryFormatError:
            continue
        if not path.is_file():
            continue
        try:
            conversation = provider.read(path, destination)
        except HistoryFormatError, OSError:
            continue
        if conversation.chat_sha256 is not None:
            revisions.setdefault(
                (conversation.logical_session_id, conversation.chat_sha256),
                relative_key,
            )
    return revisions


def _collapse_equivalent_chats(
    conversations: list[ArchivedConversation],
    *,
    destination: Path,
    provider: HistoryProvider,
) -> tuple[list[ArchivedConversation], int, int]:
    groups: dict[str, list[ArchivedConversation]] = {}
    selected = []
    for archived in conversations:
        conversation = archived.conversation
        chat_sha256 = conversation.chat_sha256
        if chat_sha256 is None:
            selected.append(archived)
        else:
            logical_revision = f"{conversation.logical_session_id}:{chat_sha256}"
            groups.setdefault(logical_revision, []).append(archived)

    current = 0
    equivalent = 0
    for group in groups.values():
        local = [
            archived
            for archived in group
            if Path(archived.source_root).expanduser().resolve() == destination
            and archived.conversation.provider == provider.name
        ]
        equivalent += len(group) - 1
        if local:
            current += len(local)
            continue
        selected.append(
            min(
                group,
                key=lambda archived: (
                    archived.conversation.provider != provider.name,
                    archived.source_root,
                    archived.conversation.relative_path,
                ),
            )
        )
    return selected, current, equivalent


def _source_key(archived: ArchivedConversation) -> str:
    conversation = archived.conversation
    identity = json.dumps(
        [conversation.provider, archived.source_root, conversation.relative_path],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _started_at(archived: ArchivedConversation) -> datetime:
    value = archived.conversation.started_at
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    return datetime.fromtimestamp(archived.source_mtime_ns / 1_000_000_000, tz=UTC)


def _safe_relative_path(path: Path) -> str:
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise HistoryFormatError(f"Provider generated an unsafe transcript path: {path}")
    return path.as_posix()


def _load_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_NAME
    if not path.exists():
        return {"version": MANIFEST_VERSION, "files": {}}
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoryFormatError(f"Could not read sync manifest {path}: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != MANIFEST_VERSION
        or not isinstance(value.get("files"), dict)
    ):
        raise HistoryFormatError(f"Unsupported or invalid sync manifest: {path}")
    return value


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    _atomic_write(root / MANIFEST_NAME, content, destination=root)


def _destination_path(destination: Path, relative_path: Path) -> tuple[str, Path]:
    relative_key = _safe_relative_path(relative_path)
    path = destination
    for part in Path(relative_key).parts:
        path /= part
        if path.is_symlink():
            raise HistoryFormatError(
                f"Refusing to write through a symlink in the destination: {path}"
            )
    return relative_key, path


def _atomic_write(path: Path, content: bytes, *, destination: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _destination_path(destination, path.relative_to(destination))
    descriptor, temporary_name = tempfile.mkstemp(prefix=".msync-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _destination_path(destination, path.relative_to(destination))
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
