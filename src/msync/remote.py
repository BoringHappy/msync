"""Wire contract for bounded, streamed remote transcript uploads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

UPLOAD_CONTENT_TYPE = "application/vnd.msync.transcript"
UPLOAD_METADATA_MAX_BYTES = 64 * 1024
UPLOAD_TRANSCRIPT_MAX_BYTES = 256 * 1024 * 1024
UPLOAD_BODY_MAX_BYTES = 4 + UPLOAD_METADATA_MAX_BYTES + UPLOAD_TRANSCRIPT_MAX_BYTES
UPLOAD_STREAM_CHUNK_BYTES = 1024 * 1024


class RemoteUploadMetadata(BaseModel):
    """Small JSON header preceding one raw native transcript."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    provider: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=255)
    root_path: str = Field(min_length=1, max_length=16_384)
    display_name: str = Field(min_length=1, max_length=255)
    relative_path: str = Field(min_length=1, max_length=4096)
    source_mtime_ns: int = Field(default=0, ge=0, le=2**63 - 1)


def encode_upload_prefix(metadata: RemoteUploadMetadata) -> bytes:
    """Encode the length-prefixed JSON metadata that starts an upload body."""

    payload = metadata.model_dump_json().encode("utf-8")
    if len(payload) > UPLOAD_METADATA_MAX_BYTES:
        raise ValueError("Remote upload metadata exceeds 64 KiB.")
    return len(payload).to_bytes(4, byteorder="big") + payload
