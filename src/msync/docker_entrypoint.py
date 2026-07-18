"""Container entrypoint helpers for constructing database connection arguments."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

from sqlalchemy import URL


def database_url(environment: Mapping[str, str]) -> str | None:
    """Build a safely encoded PostgreSQL URL from container environment fields."""

    host = environment.get("MSYNC_DATABASE_HOST")
    if not host:
        return None

    try:
        password = environment["POSTGRES_PASSWORD"]
    except KeyError as error:
        raise ValueError(
            "POSTGRES_PASSWORD is required when MSYNC_DATABASE_HOST is configured."
        ) from error

    try:
        port = int(environment.get("MSYNC_DATABASE_PORT", "5432"))
    except ValueError as error:
        raise ValueError("MSYNC_DATABASE_PORT must be an integer.") from error

    url = URL.create(
        "postgresql+psycopg",
        username=environment.get("POSTGRES_USER", "msync"),
        password=password,
        host=host,
        port=port,
        database=environment.get("POSTGRES_DB", "msync"),
    )
    return url.render_as_string(hide_password=False)


def command(
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> list[str]:
    """Return the msync command, adding a generated database URL when needed."""

    result = ["msync", *arguments]
    configured_url = database_url(environment)
    has_database_option = any(argument in {"--database", "--db"} for argument in arguments)
    if configured_url is not None and not has_database_option:
        result.extend(("--database", configured_url))
    return result


def main() -> None:
    """Replace the container entrypoint process with msync."""

    try:
        selected_command = command(sys.argv[1:], os.environ)
    except ValueError as error:
        print(f"Container startup failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    os.execvp(selected_command[0], selected_command)


if __name__ == "__main__":
    main()
