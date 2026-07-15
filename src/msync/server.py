"""Authenticated FastAPI application for browsing archived conversations."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict

from msync.database import Archive

_BASIC_SECURITY = HTTPBasic(auto_error=False)
_BasicCredentials = Annotated[HTTPBasicCredentials | None, Depends(_BASIC_SECURITY)]


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
    username: str,
    password: str,
) -> FastAPI:
    """Build an authenticated web application for one msync archive."""

    if not username:
        raise ValueError("Server username must not be empty.")
    if not password:
        raise ValueError("Server password must not be empty.")

    archive = Archive(database)

    def require_auth(credentials: _BasicCredentials) -> str:
        valid_username = credentials is not None and secrets.compare_digest(
            credentials.username.encode("utf-8"), username.encode("utf-8")
        )
        valid_password = credentials is not None and secrets.compare_digest(
            credentials.password.encode("utf-8"), password.encode("utf-8")
        )
        if not (valid_username and valid_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required.",
                headers={"WWW-Authenticate": 'Basic realm="msync", charset="UTF-8"'},
            )
        return credentials.username

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        archive.close()

    app = FastAPI(
        title="msync history browser",
        description="Browse normalized Claude and Codex chat histories.",
        version="0.1.0",
        dependencies=[Depends(require_auth)],
        lifespan=lifespan,
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
    def index() -> HTMLResponse:
        return HTMLResponse(_web_asset("index.html"))

    @app.get("/assets/styles.css", include_in_schema=False)
    def styles() -> Response:
        return Response(_web_asset("styles.css"), media_type="text/css")

    @app.get("/assets/app.js", include_in_schema=False)
    def javascript() -> Response:
        return Response(_web_asset("app.js"), media_type="text/javascript")

    @app.get("/api/locations", response_model=list[LocationResponse])
    def locations() -> list[Any]:
        return archive.browse_locations()

    @app.get("/api/conversations", response_model=list[ConversationSummaryResponse])
    def conversations(
        location: Annotated[int | None, Query(ge=1)] = None,
        search: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[Any]:
        return archive.browse_conversations(
            location_id=location,
            search_text=search,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/conversations/{conversation_id}", response_model=ConversationResponse)
    def conversation(conversation_id: int) -> Any:
        result = archive.browse_conversation(conversation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        return result

    return app


def _web_asset(name: str) -> str:
    return files("msync.web").joinpath(name).read_text(encoding="utf-8")
