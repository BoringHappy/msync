"""Authenticated FastAPI application for browsing and uploading conversations."""

from __future__ import annotations

import base64
import binascii
import secrets
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from pydantic import BaseModel, ConfigDict, Field

from msync.database import Archive, RemoteTranscript
from msync.providers import get_provider

_BASIC_SECURITY = HTTPBasic(auto_error=False)
_BasicCredentials = Annotated[HTTPBasicCredentials | None, Depends(_BASIC_SECURITY)]
_BEARER_SECURITY = HTTPBearer(auto_error=False)
_BearerCredentials = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(_BEARER_SECURITY),
]


@dataclass(slots=True, frozen=True)
class ServerAccount:
    """One browser login with an optional remote-upload token."""

    username: str
    password: str
    token: str | None = None


class TranscriptUploadRequest(BaseModel):
    """One native transcript included in a remote upload."""

    relative_path: str = Field(min_length=1, max_length=4096)
    content_base64: str = Field(max_length=140_000_000)
    source_mtime_ns: int = Field(default=0, ge=0)


class UploadRequest(BaseModel):
    """A provider location uploaded by one authenticated account."""

    version: Literal[1] = 1
    provider: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=255)
    root_path: str = Field(min_length=1, max_length=16_384)
    display_name: str = Field(min_length=1, max_length=255)
    transcripts: list[TranscriptUploadRequest] = Field(min_length=1, max_length=10_000)


class UploadResponse(BaseModel):
    """Counts produced by a remote archive upload."""

    model_config = ConfigDict(from_attributes=True)

    location_id: int
    scanned: int
    imported: int
    updated: int
    unchanged: int
    duplicates: int
    events: int
    message_parts: int


class LocationResponse(BaseModel):
    """A history location exposed by the browser API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    hostname: str
    root_path: str
    display_name: str
    last_scanned_at: datetime | None
    conversation_count: int


class ConversationSummaryResponse(BaseModel):
    """Conversation metadata used by the session picker."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    location_id: int
    provider: str
    hostname: str
    external_id: str
    title: str | None
    conversation_kind: str
    cwd: str | None
    model: str | None
    git_branch: str | None
    started_at: str | None
    ended_at: str | None
    event_count: int
    message_count: int
    preview: str | None


class MessagePartResponse(BaseModel):
    """Structured content included in an expanded event."""

    model_config = ConfigDict(from_attributes=True)

    sequence: int
    content_type: str
    text: str | None
    raw_json: str


class EventResponse(BaseModel):
    """Normalized and raw views of one transcript event."""

    model_config = ConfigDict(from_attributes=True)

    sequence: int
    external_id: str | None
    parent_external_id: str | None
    event_type: str
    event_subtype: str | None
    role: str | None
    visibility: str
    occurred_at: str | None
    text: str
    raw_json: str
    parse_error: str | None
    parts: list[MessagePartResponse]


class ConversationResponse(BaseModel):
    """Full conversation payload used by the transcript pane."""

    model_config = ConfigDict(from_attributes=True)

    summary: ConversationSummaryResponse
    relative_path: str
    parent_external_id: str | None
    metadata: dict[str, Any]
    events: list[EventResponse]


def create_app(
    database: str | Path,
    *,
    username: str | None = None,
    password: str | None = None,
    accounts: Sequence[ServerAccount] | None = None,
) -> FastAPI:
    """Build an authenticated, tenant-isolated web application for one archive."""

    if accounts is not None and (username is not None or password is not None):
        raise ValueError("Configure server accounts or a username/password pair, not both.")
    configured_accounts = (
        tuple(accounts)
        if accounts is not None
        else (ServerAccount(username or "", password or ""),)
    )
    _validate_accounts(configured_accounts)
    legacy_owner = configured_accounts[0].username

    # Long-running schema migrations belong in the explicit CLI maintenance workflow. Starting
    # the web process against an old archive should fail quickly with upgrade instructions instead
    # of silently waiting for uploads or rewriting a large archive before Uvicorn can report ready.
    archive = Archive(database, auto_upgrade=False)

    def require_auth(credentials: _BasicCredentials) -> ServerAccount:
        if credentials is not None:
            supplied_username = credentials.username.encode("utf-8")
            supplied_password = credentials.password.encode("utf-8")
            for account in configured_accounts:
                valid_username = secrets.compare_digest(
                    supplied_username,
                    account.username.encode("utf-8"),
                )
                valid_password = secrets.compare_digest(
                    supplied_password,
                    account.password.encode("utf-8"),
                )
                if valid_username and valid_password:
                    return account
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": 'Basic realm="msync", charset="UTF-8"'},
        )

    def require_upload_token(credentials: _BearerCredentials) -> ServerAccount:
        if credentials is not None and credentials.scheme.casefold() == "bearer":
            supplied_token = credentials.credentials.encode("utf-8")
            for account in configured_accounts:
                if account.token is not None and secrets.compare_digest(
                    supplied_token,
                    account.token.encode("utf-8"),
                ):
                    return account
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid upload token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        archive.close()

    app = FastAPI(
        title="msync history browser",
        description="Browse normalized Claude and Codex chat histories.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.archive = archive

    @app.middleware("http")
    async def security_headers(request: Any, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(account: ServerAccount = Depends(require_auth)) -> HTMLResponse:  # noqa: B008
        del account
        return HTMLResponse(_web_asset("index.html"))

    @app.get("/assets/styles.css", include_in_schema=False)
    def styles(account: ServerAccount = Depends(require_auth)) -> Response:  # noqa: B008
        del account
        return Response(_web_asset("styles.css"), media_type="text/css")

    @app.get("/assets/app.js", include_in_schema=False)
    def javascript(account: ServerAccount = Depends(require_auth)) -> Response:  # noqa: B008
        del account
        return Response(_web_asset("app.js"), media_type="text/javascript")

    @app.get("/api/locations", response_model=list[LocationResponse])
    def locations(
        account: ServerAccount = Depends(require_auth),  # noqa: B008
    ) -> list[Any]:
        return archive.browse_locations(
            account_username=account.username,
            include_legacy=account.username == legacy_owner,
        )

    @app.get("/api/conversations", response_model=list[ConversationSummaryResponse])
    def conversations(
        location: Annotated[int | None, Query(ge=1)] = None,
        search: Annotated[str, Query(max_length=200)] = "",
        order: Literal["newest", "oldest", "messages", "events", "title"] = "newest",
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
        account: ServerAccount = Depends(require_auth),  # noqa: B008
    ) -> list[Any]:
        return archive.browse_conversations(
            location_id=location,
            search_text=search,
            order_by=order,
            limit=limit,
            offset=offset,
            account_username=account.username,
            include_legacy=account.username == legacy_owner,
        )

    @app.post("/api/upload", response_model=UploadResponse)
    def upload(
        request: UploadRequest,
        account: ServerAccount = Depends(require_upload_token),  # noqa: B008
    ) -> Any:
        transcripts: list[RemoteTranscript] = []
        total_bytes = 0
        for item in request.transcripts:
            try:
                content = base64.b64decode(item.content_base64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid base64 transcript content for {item.relative_path!r}.",
                ) from error
            total_bytes += len(content)
            if total_bytes > 256 * 1024 * 1024:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Upload payload exceeds the 256 MiB decoded transcript limit.",
                )
            transcripts.append(
                RemoteTranscript(
                    relative_path=item.relative_path,
                    content=content,
                    source_mtime_ns=item.source_mtime_ns,
                )
            )
        try:
            return archive.upload_remote(
                root_path=request.root_path,
                display_name=request.display_name,
                provider=get_provider(request.provider),
                hostname=request.hostname,
                account_username=account.username,
                transcripts=transcripts,
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error

    @app.get("/api/conversations/{conversation_id}", response_model=ConversationResponse)
    def conversation(
        conversation_id: int,
        event_limit: Annotated[int | None, Query(ge=1, le=500)] = None,
        event_offset: Annotated[int, Query(ge=0)] = 0,
        account: ServerAccount = Depends(require_auth),  # noqa: B008
    ) -> Any:
        result = archive.browse_conversation(
            conversation_id,
            event_limit=event_limit,
            event_offset=event_offset,
            account_username=account.username,
            include_legacy=account.username == legacy_owner,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return result

    return app


def parse_server_accounts(value: str) -> tuple[ServerAccount, ...]:
    """Parse ``username,password[,token]`` records separated by semicolons."""

    if not value:
        raise ValueError("Server accounts must not be empty.")
    accounts: list[ServerAccount] = []
    for entry in value.split(";"):
        parts = entry.split(",")
        if len(parts) not in {2, 3}:
            raise ValueError(
                "Each server account must use username,password[,token] format; "
                "commas and semicolons are not allowed inside credentials."
            )
        username, password = parts[:2]
        token = parts[2] if len(parts) == 3 and parts[2] else None
        accounts.append(ServerAccount(username=username, password=password, token=token))
    configured = tuple(accounts)
    _validate_accounts(configured)
    return configured


def _validate_accounts(accounts: Sequence[ServerAccount]) -> None:
    if not accounts:
        raise ValueError("At least one server account is required.")
    usernames: set[str] = set()
    tokens: set[str] = set()
    for account in accounts:
        if not account.username:
            raise ValueError("Server username must not be empty.")
        if len(account.username) > 255:
            raise ValueError("Server username must not exceed 255 characters.")
        if not account.password:
            raise ValueError("Server password must not be empty.")
        for field_name, value in (
            ("username", account.username),
            ("password", account.password),
            ("token", account.token),
        ):
            if value is not None and any(delimiter in value for delimiter in (",", ";")):
                raise ValueError(
                    f"Server account {field_name} must not contain ',' or ';'."
                )
        if account.username in usernames:
            raise ValueError(f"Duplicate server username {account.username!r}.")
        usernames.add(account.username)
        if account.token is not None:
            if not account.token:
                raise ValueError("Server upload token must not be empty.")
            if account.token in tokens:
                raise ValueError("Server upload tokens must be unique.")
            tokens.add(account.token)


def _web_asset(name: str) -> str:
    return files("msync.web").joinpath(name).read_text(encoding="utf-8")
