"""Shared behavior for evolving provider-owned JSON schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class NativeRecord(BaseModel):
    """Accept new provider fields while validating the fields msync understands."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class StrictNativeRecord(NativeRecord):
    """Reject undeclared fields in records emitted by msync."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
