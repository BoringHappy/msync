"""Authenticated FastAPI application for browsing and uploading conversations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from html import escape
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from msync.database import Archive, RemoteTranscript
from msync.providers import get_provider
from msync.remote import (
    UPLOAD_BODY_MAX_BYTES,
    UPLOAD_CONTENT_TYPE,
    UPLOAD_METADATA_MAX_BYTES,
    UPLOAD_STREAM_CHUNK_BYTES,
    UPLOAD_TRANSCRIPT_MAX_BYTES,
    RemoteUploadMetadata,
)

_BASIC_SECURITY = HTTPBasic(auto_error=False)
_BasicCredentials = Annotated[HTTPBasicCredentials | None, Depends(_BASIC_SECURITY)]
_BEARER_SECURITY = HTTPBearer(auto_error=False)
_BearerCredentials = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(_BEARER_SECURITY),
]
_SESSION_COOKIE = "msync_session"
_CSRF_COOKIE = "msync_csrf"
_SESSION_MAX_AGE_SECONDS = 60 * 60 * 12
_FORM_BODY_MAX_BYTES = 64 * 1024
_BROWSER_REQUEST_HEADER = "x-msync-browser-request"
_BASIC_CHALLENGE = 'Basic realm="msync", charset="UTF-8"'


@dataclass(slots=True, frozen=True)
class ServerAccount:
    """One browser login with an optional remote-upload token."""

    username: str
    password: str
    token: str | None = None


class _UploadBodyTooLarge(Exception):
    """Stop reading an upload as soon as it crosses the request limit."""


class _FormBodyError(Exception):
    """Reject invalid or oversized browser form requests."""

    def __init__(self, detail: str, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class UploadBodyLimitMiddleware:
    """Reject oversized upload bodies before FastAPI buffers or parses them."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/api/upload":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", ()))
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                content_length = 0
            if content_length > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _UploadBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _UploadBodyTooLarge:
            if response_started:
                raise
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": "Upload request body exceeds the 256 MiB transcript limit."},
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
        await response(scope, receive, send)


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
    accounts_by_username = {account.username: account for account in configured_accounts}
    session_secret = secrets.token_bytes(32)

    # Long-running schema migrations belong in the explicit CLI maintenance workflow. Starting
    # the web process against an old archive should fail quickly with upgrade instructions instead
    # of silently waiting for uploads or rewriting a large archive before Uvicorn can report ready.
    archive = Archive(database, auto_upgrade=False)

    def authenticate_credentials(username: str, password: str) -> ServerAccount | None:
        supplied_username = username.encode("utf-8")
        supplied_password = password.encode("utf-8")
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
        return None

    def session_cookie(account: ServerAccount) -> str:
        payload = json.dumps(
            {
                "expires": int(time.time()) + _SESSION_MAX_AGE_SECONDS,
                "username": account.username,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.digest(session_secret, payload, hashlib.sha256)
        return f"{_base64url_encode(payload)}.{_base64url_encode(signature)}"

    def session_account(request: Request) -> ServerAccount | None:
        value = request.cookies.get(_SESSION_COOKIE)
        if value is None or len(value) > 4096:
            return None
        try:
            encoded_payload, encoded_signature = value.split(".", maxsplit=1)
            payload = _base64url_decode(encoded_payload)
            supplied_signature = _base64url_decode(encoded_signature)
        except ValueError, binascii.Error:
            return None
        expected_signature = hmac.digest(session_secret, payload, hashlib.sha256)
        if not secrets.compare_digest(supplied_signature, expected_signature):
            return None
        try:
            values = json.loads(payload)
        except UnicodeDecodeError, json.JSONDecodeError:
            return None
        username = values.get("username") if isinstance(values, dict) else None
        expires = values.get("expires") if isinstance(values, dict) else None
        if not isinstance(username, str) or not isinstance(expires, int):
            return None
        if expires <= int(time.time()):
            return None
        return accounts_by_username.get(username)

    def valid_csrf_token(value: str) -> bool:
        if not value or len(value) > 256:
            return False
        try:
            nonce, encoded_signature = value.split(".", maxsplit=1)
            supplied_signature = _base64url_decode(encoded_signature)
        except ValueError, binascii.Error:
            return False
        if not nonce:
            return False
        expected_signature = hmac.digest(
            session_secret,
            f"csrf:{nonce}".encode(),
            hashlib.sha256,
        )
        return secrets.compare_digest(supplied_signature, expected_signature)

    def csrf_token(request: Request) -> str:
        existing = request.cookies.get(_CSRF_COOKIE, "")
        if valid_csrf_token(existing):
            return existing
        nonce = secrets.token_urlsafe(32)
        signature = hmac.digest(
            session_secret,
            f"csrf:{nonce}".encode(),
            hashlib.sha256,
        )
        return f"{nonce}.{_base64url_encode(signature)}"

    def valid_csrf_submission(request: Request, submitted: str) -> bool:
        cookie = request.cookies.get(_CSRF_COOKIE, "")
        return (
            valid_csrf_token(cookie)
            and bool(submitted)
            and secrets.compare_digest(cookie.encode(), submitted.encode())
        )

    def set_csrf_cookie(response: Response, request: Request, value: str) -> None:
        response.set_cookie(
            key=_CSRF_COOKIE,
            value=value,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )

    def login_response(
        request: Request,
        destination: str,
        error: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        token = csrf_token(request)
        response = HTMLResponse(
            _login_page(destination, token, error),
            status_code=status_code,
        )
        set_csrf_cookie(response, request, token)
        return response

    def authenticated_account(
        request: Request,
        credentials: _BasicCredentials,
    ) -> ServerAccount | None:
        if "authorization" in request.headers:
            if credentials is None:
                return None
            return authenticate_credentials(credentials.username, credentials.password)
        return session_account(request)

    def require_auth(request: Request, credentials: _BasicCredentials) -> ServerAccount:
        account = authenticated_account(request, credentials)
        if account is not None:
            return account
        challenge_headers = None
        if request.headers.get(_BROWSER_REQUEST_HEADER) != "1":
            challenge_headers = {"WWW-Authenticate": _BASIC_CHALLENGE}
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers=challenge_headers,
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
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=UPLOAD_BODY_MAX_BYTES)

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

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(
        request: Request,
        credentials: _BasicCredentials,
        next_path: Annotated[str, Query(alias="next", max_length=2048)] = "/",
    ) -> Response:
        destination = _safe_next_path(next_path)
        if authenticated_account(request, credentials) is not None:
            return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
        return login_response(request, destination)

    @app.post("/login", response_class=HTMLResponse, include_in_schema=False)
    async def log_in(request: Request) -> Response:
        try:
            fields = await _read_form_fields(request)
        except _FormBodyError as error:
            return login_response(
                request,
                "/",
                error.detail,
                error.status_code,
            )
        username = _single_form_value(fields, "username")
        password = _single_form_value(fields, "password")
        destination = _safe_next_path(_single_form_value(fields, "next"))
        if not valid_csrf_submission(request, _single_form_value(fields, "csrf_token")):
            return login_response(
                request,
                destination,
                "The sign-in form expired. Refresh the page and try again.",
                status.HTTP_403_FORBIDDEN,
            )
        account = authenticate_credentials(username, password)
        if account is None:
            return login_response(
                request,
                destination,
                "The username or password is incorrect.",
                status.HTTP_401_UNAUTHORIZED,
            )
        response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            key=_SESSION_COOKIE,
            value=session_cookie(account),
            max_age=_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
        )
        return response

    @app.post("/logout", include_in_schema=False)
    async def log_out(request: Request) -> Response:
        try:
            fields = await _read_form_fields(request)
        except _FormBodyError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail) from error
        if not valid_csrf_submission(request, _single_form_value(fields, "csrf_token")):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or expired CSRF token.",
            )
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(_SESSION_COOKIE, httponly=True, samesite="lax")
        response.delete_cookie(_CSRF_COOKIE, httponly=True, samesite="strict")
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request, credentials: _BasicCredentials) -> Response:
        if authenticated_account(request, credentials) is None:
            destination = request.url.path
            if request.url.query:
                destination = f"{destination}?{request.url.query}"
            location = f"/login?{urlencode({'next': destination})}"
            return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
        token = csrf_token(request)
        response = HTMLResponse(_index_page(token))
        set_csrf_cookie(response, request, token)
        return response

    @app.get("/assets/styles.css", include_in_schema=False)
    def styles() -> Response:
        return Response(_web_asset("styles.css"), media_type="text/css")

    @app.get("/assets/app.js", include_in_schema=False)
    def javascript() -> Response:
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
    async def upload(
        request: Request,
        account: ServerAccount = Depends(require_upload_token),  # noqa: B008
    ) -> Any:
        metadata, content = await _read_upload_request(request)
        try:
            return await run_in_threadpool(
                archive.upload_remote,
                root_path=metadata.root_path,
                display_name=metadata.display_name,
                provider=get_provider(metadata.provider),
                hostname=metadata.hostname,
                account_username=account.username,
                transcripts=[
                    RemoteTranscript(
                        relative_path=metadata.relative_path,
                        content=content,
                        source_mtime_ns=metadata.source_mtime_ns,
                    )
                ],
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


async def _read_upload_request(request: Request) -> tuple[RemoteUploadMetadata, bytes]:
    content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip()
    if content_type.casefold() != UPLOAD_CONTENT_TYPE:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Remote uploads require Content-Type: {UPLOAD_CONTENT_TYPE}.",
        )

    with tempfile.SpooledTemporaryFile(max_size=UPLOAD_STREAM_CHUNK_BYTES, mode="w+b") as body:
        buffered = bytearray()
        async for chunk in request.stream():
            buffered.extend(chunk)
            if len(buffered) >= UPLOAD_STREAM_CHUNK_BYTES:
                await run_in_threadpool(body.write, bytes(buffered))
                buffered.clear()
        if buffered:
            await run_in_threadpool(body.write, bytes(buffered))
        body.seek(0)
        prefix = body.read(4)
        if len(prefix) != 4:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Remote upload body is missing its metadata length prefix.",
            )
        metadata_length = int.from_bytes(prefix, byteorder="big")
        if metadata_length < 1 or metadata_length > UPLOAD_METADATA_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Remote upload metadata length is invalid.",
            )
        metadata_payload = body.read(metadata_length)
        if len(metadata_payload) != metadata_length:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Remote upload metadata is truncated.",
            )
        try:
            metadata = RemoteUploadMetadata.model_validate_json(metadata_payload)
        except ValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Remote upload metadata is invalid: {error}",
            ) from error
        content = await run_in_threadpool(body.read, UPLOAD_TRANSCRIPT_MAX_BYTES + 1)
        if len(content) > UPLOAD_TRANSCRIPT_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Transcript exceeds the 256 MiB upload limit.",
            )
    return metadata, content


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
                raise ValueError(f"Server account {field_name} must not contain ',' or ';'.")
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


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(f"{value}{padding}", altchars=b"-_", validate=True)


def _safe_next_path(value: str) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(character) < 32 for character in value):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path in {"/login", "/logout"}:
        return "/"
    return value


def _single_form_value(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name, [])
    return values[0] if len(values) == 1 else ""


async def _read_form_fields(request: Request) -> dict[str, list[str]]:
    content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
    if content_type.casefold() != "application/x-www-form-urlencoded":
        raise _FormBodyError(
            "Submit the browser form to continue.",
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise _FormBodyError(
                "The form request is invalid.",
                status.HTTP_400_BAD_REQUEST,
            ) from error
        if declared_length < 0:
            raise _FormBodyError(
                "The form request is invalid.",
                status.HTTP_400_BAD_REQUEST,
            )
        if declared_length > _FORM_BODY_MAX_BYTES:
            raise _FormBodyError(
                "The form request is too large.",
                status.HTTP_413_CONTENT_TOO_LARGE,
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _FORM_BODY_MAX_BYTES:
            raise _FormBodyError(
                "The form request is too large.",
                status.HTTP_413_CONTENT_TOO_LARGE,
            )
        body.extend(chunk)
    try:
        return parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=10,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise _FormBodyError(
            "The form request is invalid.",
            status.HTTP_400_BAD_REQUEST,
        ) from error


def _index_page(csrf_token: str) -> str:
    return _web_asset("index.html").replace("__MSYNC_CSRF__", escape(csrf_token, quote=True))


def _login_page(next_path: str, csrf_token: str, error: str | None = None) -> str:
    error_markup = ""
    if error is not None:
        error_markup = (
            '<div class="login-error" role="alert">'
            '<span aria-hidden="true">!</span>'
            f"<span>{escape(error)}</span>"
            "</div>"
        )
    return (
        _web_asset("login.html")
        .replace("__MSYNC_LOGIN_ERROR__", error_markup)
        .replace("__MSYNC_CSRF__", escape(csrf_token, quote=True))
        .replace("__MSYNC_NEXT__", escape(next_path, quote=True))
    )
