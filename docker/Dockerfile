# syntax=docker/dockerfile:1

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.14-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/BoringHappy/msync" \
      org.opencontainers.image.description="Archive and search local AI chat histories" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PATH="/app/.venv/bin:$PATH" \
    MSYNC_DATABASE_URL="/data/msync.sqlite" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 msync \
    && useradd --uid 10001 --gid msync --create-home --home-dir /home/msync \
        --shell /usr/sbin/nologin msync \
    && install --directory --owner=msync --group=msync /app /data

COPY --from=builder --chown=msync:msync /app/.venv /app/.venv

USER msync
WORKDIR /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8000), 2).close()"]

ENTRYPOINT ["python", "-m", "msync.docker_entrypoint"]
CMD ["server", "--host", "0.0.0.0"]
