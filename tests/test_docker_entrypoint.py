import os

import pytest
from sqlalchemy import make_url

from msync.docker_entrypoint import command, database_url, main


def test_database_url_preserves_reserved_password_characters() -> None:
    password = "secret@:/% value"

    rendered = database_url(
        {
            "MSYNC_DATABASE_HOST": "postgres",
            "POSTGRES_USER": "msync",
            "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": "archive",
        }
    )

    assert rendered is not None
    parsed = make_url(rendered)
    assert parsed.password == password
    assert parsed.host == "postgres"
    assert parsed.database == "archive"


def test_command_keeps_database_configuration_out_of_arguments() -> None:
    selected = command(
        ["server", "--host", "0.0.0.0"],
        {
            "MSYNC_DATABASE_HOST": "postgres",
            "POSTGRES_PASSWORD": "secret",
        },
    )

    assert selected == ["msync", "server", "--host", "0.0.0.0"]


def test_command_does_not_accept_database_configuration_arguments() -> None:
    selected = command(
        ["server", "--host", "127.0.0.1"],
        {
            "MSYNC_DATABASE_HOST": "postgres",
            "POSTGRES_PASSWORD": "secret",
        },
    )

    assert selected == ["msync", "server", "--host", "127.0.0.1"]


def test_main_exposes_generated_database_url_through_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    environment = {
        "MSYNC_DATABASE_HOST": "postgres",
        "POSTGRES_PASSWORD": "secret",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MSYNC_DATABASE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["msync-entrypoint", "server"])

    def fake_execvp(executable: str, arguments: list[str]) -> None:
        captured.update(
            executable=executable,
            arguments=arguments,
            database_url=os.environ["MSYNC_DATABASE_URL"],
        )

    monkeypatch.setattr("os.execvp", fake_execvp)

    main()

    assert captured["executable"] == "msync"
    assert captured["arguments"] == ["msync", "server"]
    assert make_url(str(captured["database_url"])).password == "secret"
