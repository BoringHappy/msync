from sqlalchemy import make_url

from msync.docker_entrypoint import command, database_url


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


def test_command_adds_generated_database_url() -> None:
    selected = command(
        ["server", "--host", "0.0.0.0"],
        {
            "MSYNC_DATABASE_HOST": "postgres",
            "POSTGRES_PASSWORD": "secret",
        },
    )

    assert selected[:4] == ["msync", "server", "--host", "0.0.0.0"]
    assert selected[4] == "--database"
    assert make_url(selected[5]).password == "secret"


def test_command_preserves_explicit_database_url() -> None:
    selected = command(
        ["server", "--database", "sqlite:////data/archive.sqlite"],
        {
            "MSYNC_DATABASE_HOST": "postgres",
            "POSTGRES_PASSWORD": "secret",
        },
    )

    assert selected == ["msync", "server", "--database", "sqlite:////data/archive.sqlite"]
