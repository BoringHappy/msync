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
SESSION_NAMESPACE = UUID("bf0e673b-33eb-4b72-aed5-a34c11b4a2b6")


@dataclass(slots=True, frozen=True)
class SyncResult:
    """File outcomes for one native destination."""

    current: int = 0
    written: int = 0
    unchanged: int = 0
    protected: int = 0
    skipped: int = 0
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
    current = written = unchanged = protected = skipped = 0
    conflicts: list[str] = []

    for archived in conversations:
        conversation = archived.conversation
        source_root = Path(archived.source_root).expanduser().resolve()
        if source_root == destination and conversation.provider == provider.name:
            current += 1
            continue

        source_key = _source_key(archived)
        session_id = str(uuid5(SESSION_NAMESPACE, source_key))
        started_at = _started_at(archived)
        if conversation.provider == provider.name:
            relative_path = Path(conversation.relative_path)
            content = conversation.transcript
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

        relative_key = _safe_relative_path(relative_path)
        output_path = destination / relative_key
        expected_hash = hashlib.sha256(content).hexdigest()
        entry = files.get(relative_key)
        if not isinstance(entry, dict):
            entry = None
        sources = set(entry.get("sources", [])) if entry else set()
        recorded_hash = entry.get("sha256") if entry else None

        if output_path.is_symlink():
            conflicts.append(relative_key)
            continue
        if output_path.exists():
            actual_hash = _file_hash(output_path)
            if actual_hash == expected_hash:
                unchanged += 1
            elif source_key in sources and actual_hash != recorded_hash:
                protected += 1
                continue
            elif source_key in sources and len(sources) == 1 and actual_hash == recorded_hash:
                _atomic_write(output_path, content)
                written += 1
            else:
                conflicts.append(relative_key)
                continue
        else:
            _atomic_write(output_path, content)
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
        conflicts=tuple(conflicts),
    )


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
    _atomic_write(root / MANIFEST_NAME, content)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".msync-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
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
