"""Safe, idempotent synchronization into native provider history directories."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from msync.database import ArchivedConversation
from msync.models import Conversation
from msync.providers import (
    HistoryFormatError,
    HistoryProvider,
    NoTransferableMessagesError,
)

MANIFEST_NAME = ".msync-manifest.json"
MANIFEST_VERSION = 2
_READABLE_MANIFEST_VERSIONS = frozenset({1, MANIFEST_VERSION})
LOCK_NAME = ".msync.lock"
WRITER_SCHEMA_VERSION = 1

try:
    import fcntl as _posix_lock
except ImportError:  # pragma: no cover - exercised on Windows.
    _posix_lock = None

try:
    import msvcrt as _windows_lock
except ImportError:  # pragma: no cover - exercised on POSIX.
    _windows_lock = None


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


@dataclass(slots=True, frozen=True)
class _PendingWrite:
    """One validated transcript that was absent while building the write plan."""

    relative_key: str
    output_path: Path
    content: bytes
    expected_hash: str
    source_key: str
    recognized_source_keys: frozenset[str]
    sources: frozenset[str]
    provider: str
    external_id: str
    logical_session_id: str
    chat_sha256: str | None
    generated: bool


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


def managed_transcript_logical_session_id(
    root: Path,
    path: Path,
    *,
    provider: HistoryProvider,
) -> str | None:
    """Return sidecar identity only while the path still represents the managed session."""

    try:
        manifest = _load_manifest(root)
        relative_path = path.relative_to(root).as_posix()
    except HistoryFormatError, ValueError:
        return None
    entry = manifest["files"].get(relative_path)
    if not isinstance(entry, dict):
        return None
    expected_external_id = entry.get("external_id")
    if entry.get("provider") != provider.name or not isinstance(expected_external_id, str):
        return None
    value = entry.get("logical_session_id")
    if not isinstance(value, str):
        return None
    try:
        logical_session_id = str(UUID(value))
        conversation = provider.read(path, root)
    except HistoryFormatError, OSError, ValueError:
        return None
    if conversation.external_id != expected_external_id:
        return None
    return logical_session_id


def sync_conversations(
    conversations: list[ArchivedConversation],
    *,
    destination: Path,
    provider: HistoryProvider,
    current_hostname: str,
) -> SyncResult:
    """Write all archived histories into one provider-native destination."""

    destination = destination.expanduser().resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _destination_lock(destination):
        return _sync_conversations_locked(
            conversations,
            destination=destination,
            provider=provider,
            current_hostname=current_hostname,
        )


def _sync_conversations_locked(
    conversations: list[ArchivedConversation],
    *,
    destination: Path,
    provider: HistoryProvider,
    current_hostname: str,
) -> SyncResult:
    """Plan, validate, and commit one destination while holding its manifest lock."""

    manifest = _load_manifest(destination)
    files: dict[str, dict[str, Any]] = manifest["files"]
    existing_revisions = _manifest_revision_paths(destination, provider, files)
    selected, current, equivalent = _collapse_equivalent_chats(
        conversations,
        destination=destination,
        provider=provider,
        current_hostname=current_hostname,
    )
    written = unchanged = protected = skipped = 0
    conflicts: list[str] = []
    pending: list[_PendingWrite] = []

    for archived in selected:
        conversation = archived.conversation
        if _is_current_location(
            archived,
            destination=destination,
            provider=provider,
            current_hostname=current_hostname,
        ):
            current += 1
            continue

        source_key = _source_key(archived)
        recognized_source_keys = {source_key, _legacy_source_key(archived)}
        revision_hash = conversation.chat_sha256 or conversation.sha256
        revision_identity = (conversation.logical_session_id, conversation.chat_sha256)
        legacy_path = existing_revisions.get(revision_identity)
        if conversation.chat_sha256 is not None and legacy_path is not None:
            entry = files[legacy_path]
            sources = set(entry.get("sources", []))
            sources.add(source_key)
            _update_manifest_entry(
                entry,
                expected_hash=entry.get("sha256"),
                sources=sources,
                provider=provider.name,
                external_id=entry.get("external_id"),
                logical_session_id=conversation.logical_session_id,
                chat_sha256=conversation.chat_sha256,
                generated=bool(entry.get("generated", True)),
            )
            unchanged += 1
            continue
        session_id = str(uuid5(UUID(conversation.logical_session_id), revision_hash))
        started_at = _started_at(archived)
        generated = False
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
                except NoTransferableMessagesError:
                    skipped += 1
                    continue
                generated = True
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
            except NoTransferableMessagesError:
                skipped += 1
                continue
            generated = True
            relative_path = provider.export_relative_path(
                conversation,
                session_id=session_id,
                started_at=started_at,
            )

        relative_key, output_path = _destination_path(destination, relative_path)
        validated = provider.validate_transcript(
            output_path,
            destination,
            transcript=content,
            logical_session_id=conversation.logical_session_id,
            strict_export=generated,
        )
        _verify_candidate_identity(
            validated,
            source=conversation,
            expected_external_id=session_id if generated else conversation.external_id,
        )
        expected_hash = hashlib.sha256(content).hexdigest()
        entry = files.get(relative_key)
        if not isinstance(entry, dict):
            entry = None
        sources = set(entry.get("sources", [])) if entry else set()

        if output_path.exists():
            if output_path.is_symlink() or not output_path.is_file():
                conflicts.append(relative_key)
                continue
            actual_hash = _file_hash(output_path)
            if actual_hash == expected_hash:
                unchanged += 1
                sources.add(source_key)
                files[relative_key] = _new_manifest_entry(
                    expected_hash=expected_hash,
                    sources=sources,
                    provider=provider.name,
                    external_id=validated.external_id,
                    logical_session_id=conversation.logical_session_id,
                    chat_sha256=conversation.chat_sha256,
                    generated=generated,
                )
            elif recognized_source_keys.intersection(sources):
                protected += 1
                continue
            else:
                conflicts.append(relative_key)
                continue
        else:
            pending.append(
                _PendingWrite(
                    relative_key=relative_key,
                    output_path=output_path,
                    content=content,
                    expected_hash=expected_hash,
                    source_key=source_key,
                    recognized_source_keys=frozenset(recognized_source_keys),
                    sources=frozenset(sources),
                    provider=provider.name,
                    external_id=validated.external_id,
                    logical_session_id=conversation.logical_session_id,
                    chat_sha256=conversation.chat_sha256,
                    generated=generated,
                )
            )

    created: list[_PendingWrite] = []
    try:
        for candidate in pending:
            if _atomic_create(
                candidate.output_path,
                candidate.content,
                destination=destination,
            ):
                written += 1
                created.append(candidate)
                sources = set(candidate.sources)
                sources.add(candidate.source_key)
                files[candidate.relative_key] = _new_manifest_entry(
                    expected_hash=candidate.expected_hash,
                    sources=sources,
                    provider=candidate.provider,
                    external_id=candidate.external_id,
                    logical_session_id=candidate.logical_session_id,
                    chat_sha256=candidate.chat_sha256,
                    generated=candidate.generated,
                )
                continue

            outcome = _classify_raced_target(candidate, files)
            if outcome == "unchanged":
                unchanged += 1
            elif outcome == "protected":
                protected += 1
            else:
                conflicts.append(candidate.relative_key)
        _write_manifest(destination, manifest)
    except BaseException:
        _rollback_created_transcripts(created)
        raise
    return SyncResult(
        current=current,
        written=written,
        unchanged=unchanged,
        protected=protected,
        skipped=skipped,
        equivalent=equivalent,
        conflicts=tuple(conflicts),
    )


def _verify_candidate_identity(
    validated: Conversation,
    *,
    source: Conversation,
    expected_external_id: str,
) -> None:
    """Ensure schema-valid bytes still represent exactly the intended chat revision."""

    if validated.external_id != expected_external_id:
        raise HistoryFormatError(
            "Generated transcript session identity changed during provider round-trip."
        )
    if validated.logical_session_id != source.logical_session_id:
        raise HistoryFormatError(
            "Generated transcript logical identity changed during provider round-trip."
        )
    if validated.chat_sha256 != source.chat_sha256:
        raise HistoryFormatError(
            "Generated transcript message content changed during provider round-trip."
        )


def _new_manifest_entry(
    *,
    expected_hash: str,
    sources: set[str],
    provider: str,
    external_id: str,
    logical_session_id: str,
    chat_sha256: str | None,
    generated: bool,
) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    _update_manifest_entry(
        entry,
        expected_hash=expected_hash,
        sources=sources,
        provider=provider,
        external_id=external_id,
        logical_session_id=logical_session_id,
        chat_sha256=chat_sha256,
        generated=generated,
    )
    return entry


def _update_manifest_entry(
    entry: dict[str, Any],
    *,
    expected_hash: Any,
    sources: set[str],
    provider: str,
    external_id: Any,
    logical_session_id: str,
    chat_sha256: str | None,
    generated: bool,
) -> None:
    if isinstance(expected_hash, str):
        entry["sha256"] = expected_hash
    entry["sources"] = sorted(source for source in sources if isinstance(source, str))
    entry["provider"] = provider
    if isinstance(external_id, str):
        entry["external_id"] = external_id
    entry["logical_session_id"] = logical_session_id
    entry["chat_sha256"] = chat_sha256
    entry["generated"] = generated
    if generated:
        entry["writer_schema_version"] = WRITER_SCHEMA_VERSION
    else:
        entry.pop("writer_schema_version", None)


def _classify_raced_target(
    candidate: _PendingWrite,
    files: dict[str, dict[str, Any]],
) -> str:
    """Classify a path that appeared after planning without ever replacing it."""

    path = candidate.output_path
    if path.is_symlink() or not path.is_file() or _file_hash(path) != candidate.expected_hash:
        entry = files.get(candidate.relative_key)
        sources = set(entry.get("sources", [])) if isinstance(entry, dict) else set()
        if candidate.recognized_source_keys.intersection(sources):
            return "protected"
        return "conflict"
    sources = set(candidate.sources)
    sources.add(candidate.source_key)
    files[candidate.relative_key] = _new_manifest_entry(
        expected_hash=candidate.expected_hash,
        sources=sources,
        provider=candidate.provider,
        external_id=candidate.external_id,
        logical_session_id=candidate.logical_session_id,
        chat_sha256=candidate.chat_sha256,
        generated=candidate.generated,
    )
    return "unchanged"


def _rollback_created_transcripts(created: list[_PendingWrite]) -> None:
    """Remove only untouched files created by the failed manifest transaction."""

    for candidate in reversed(created):
        path = candidate.output_path
        with suppress(OSError):
            if path.is_file() and _file_hash(path) == candidate.expected_hash:
                path.unlink()


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
            logical_session_id = entry.get("logical_session_id")
            conversation = provider.read(
                path,
                destination,
                logical_session_id=(
                    logical_session_id if isinstance(logical_session_id, str) else None
                ),
            )
        except HistoryFormatError, OSError:
            continue
        if conversation.chat_sha256 is not None:
            entry["external_id"] = conversation.external_id
            entry["provider"] = provider.name
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
    current_hostname: str,
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
            if _is_current_location(
                archived,
                destination=destination,
                provider=provider,
                current_hostname=current_hostname,
            )
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
        [
            conversation.provider,
            archived.source_hostname.casefold(),
            archived.source_root,
            conversation.relative_path,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _legacy_source_key(archived: ArchivedConversation) -> str:
    """Return the pre-hostname key so existing manifests still protect continued sessions."""

    conversation = archived.conversation
    identity = json.dumps(
        [conversation.provider, archived.source_root, conversation.relative_path],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _is_current_location(
    archived: ArchivedConversation,
    *,
    destination: Path,
    provider: HistoryProvider,
    current_hostname: str,
) -> bool:
    """Match a native source only when both its host and path identify this destination."""

    return (
        archived.source_hostname.casefold() == current_hostname.casefold()
        and Path(archived.source_root).expanduser().resolve() == destination
        and archived.conversation.provider == provider.name
    )


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
        or value.get("version") not in _READABLE_MANIFEST_VERSIONS
        or not isinstance(value.get("files"), dict)
    ):
        raise HistoryFormatError(f"Unsupported or invalid sync manifest: {path}")
    # Version 1 embedded msync identity in generated native records. Version 2
    # keeps native records clean and carries that identity in the manifest.
    # Read version 1 for migration, but always write version 2 so older clients
    # fail safely instead of silently dropping the sidecar identity.
    return value


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    manifest["version"] = MANIFEST_VERSION
    content = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    _atomic_replace(root / MANIFEST_NAME, content, destination=root)


def _destination_path(
    destination: Path,
    relative_path: Path,
    *,
    allow_reserved: bool = False,
) -> tuple[str, Path]:
    relative_key = _safe_relative_path(relative_path)
    if not allow_reserved and relative_key in {MANIFEST_NAME, LOCK_NAME}:
        raise HistoryFormatError(f"Provider generated an unsafe transcript path: {relative_path}")
    path = destination
    for part in Path(relative_key).parts:
        path /= part
        if path.is_symlink():
            raise HistoryFormatError(
                f"Refusing to write through a symlink in the destination: {path}"
            )
    return relative_key, path


@contextmanager
def _destination_lock(destination: Path) -> Iterator[None]:
    """Serialize msync writers so manifest updates cannot clobber each other."""

    _, path = _destination_path(destination, Path(LOCK_NAME), allow_reserved=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if _posix_lock is not None:
            _posix_lock.flock(descriptor, _posix_lock.LOCK_EX)
        elif _windows_lock is not None:  # pragma: no cover - exercised on Windows.
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            _windows_lock.locking(descriptor, _windows_lock.LK_LOCK, 1)
        else:  # pragma: no cover - supported Python platforms provide one implementation.
            raise RuntimeError("This platform does not provide a supported file lock.")
        yield
    finally:
        if _posix_lock is not None:
            with suppress(OSError):
                _posix_lock.flock(descriptor, _posix_lock.LOCK_UN)
        elif _windows_lock is not None:  # pragma: no cover - exercised on Windows.
            with suppress(OSError):
                os.lseek(descriptor, 0, os.SEEK_SET)
                _windows_lock.locking(descriptor, _windows_lock.LK_UNLCK, 1)
        os.close(descriptor)


def _atomic_create(path: Path, content: bytes, *, destination: Path) -> bool:
    """Atomically create a transcript without replacing a path that appears concurrently."""

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
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError:
            return False
        return True
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_replace(path: Path, content: bytes, *, destination: Path) -> None:
    """Atomically replace an msync-owned metadata file."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _destination_path(destination, path.relative_to(destination), allow_reserved=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".msync-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _destination_path(destination, path.relative_to(destination), allow_reserved=True)
        os.replace(temporary_path, path)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    finally:
        temporary_path.unlink(missing_ok=True)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
