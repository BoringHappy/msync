# msync

Keep your Claude Code and Codex conversations in one searchable archive.

`msync` preserves the original transcripts, backs them up to your own server, and provides a
private web UI for searching and browsing them. It can also keep Claude Code and Codex history in
sync so sessions remain available in both clients.

## Quick start

### 1. Start the server with Docker

Clone the repository and start msync with its bundled PostgreSQL database:

```console
$ git clone https://github.com/BoringHappy/msync.git
$ cd msync
$ export MSYNC_SERVER_ACCOUNTS='alice,web-password,access-token'
$ export POSTGRES_PASSWORD='choose-a-strong-database-password'
$ docker compose -f docker/docker-compose.yaml up -d
```

Open <http://localhost:8000> and sign in with username `alice` and password `web-password`. The
third account value, `access-token`, is the token used by clients to upload history.

### 2. Install and configure a client

The client requires Python 3.14+ and [uv](https://docs.astral.sh/uv/):

```console
$ uv tool install --upgrade git+https://github.com/BoringHappy/msync.git
$ export MSYNC_ENDPOINT='http://localhost:8000'
$ export MSYNC_TOKEN='access-token'
```

Use the server's reachable URL instead of `http://localhost:8000` when the client runs on another
machine. Use HTTPS whenever the server is exposed over a network.

### 3. Upload and search history

These are the main client commands:

```console
# Upload new or changed Claude Code and Codex sessions to the configured server.
$ msync upload --dir ~/.claude
$ msync upload --dir ~/.codex

# Specify the provider when a custom directory cannot be detected automatically.
$ msync upload --dir /mnt/claude-history --provider claude

# If environment variables are unavailable, pass credentials directly.
# Explicit options take priority over MSYNC_ENDPOINT and MSYNC_TOKEN.
$ msync upload --dir ~/.claude --url https://history.example.com --token alice-token

# Upload local changes, merge the remote archive into both clients, and search the server.
$ msync sync --dir ~/.claude --dir ~/.codex
$ msync search "database migration"
$ msync sample 5

# Show every option for a command.
$ msync COMMAND --help
```

All client commands use the server configured by `MSYNC_ENDPOINT` and authenticate with
`MSYNC_TOKEN`. Pass `--url` and `--token` explicitly only when environment variables are
unavailable.

`upload` only reads the source directory. It verifies transcripts before sending them and uploads
only new or changed sessions; failed transcripts do not prevent other valid transcripts from
uploading. Each transcript can be up to 256 MiB.

Environment variables are recommended for regular use because a token passed with `--token` may be
saved in shell history. Access tokens can upload and read an account's archive, so protect them and
the archive contents like the original history directories.

## Automatic uploads

The included plugin installs or upgrades `msync` when a Claude Code or Codex session starts, then
queues the current session for upload after every turn. Repeated events are safe and upload hooks
return immediately.

Make sure `MSYNC_ENDPOINT` and `MSYNC_TOKEN` are present in the environment that launches the
client. Add them to the appropriate shell startup or service configuration if uploads must continue
after a restart.

For Claude Code:

```console
$ claude plugin marketplace add BoringHappy/msync
$ claude plugin install msync@msync
```

For Codex:

```console
$ codex plugin marketplace add BoringHappy/msync
$ codex plugin add msync@msync
```

After installing the Codex plugin, open `/hooks`, review the `msync upload-hook` command, and trust
it. The plugin does nothing when either required environment variable is missing.

The plugin also includes the `recall-history` skill for Claude Code and Codex. Ask the agent to use
`recall-history` when you want to find an earlier decision or investigation. The skill can limit
results to the current repository and never passes the token on the command line.

## Local synchronization

`msync sync` performs three operations:

1. Uploads new or changed local conversations to `MSYNC_ENDPOINT`.
2. Downloads the account's archive and adds each conversation to the other client's history in its
   native format.
3. Keeps sessions separate and resumable.

Running it again is safe: unchanged and duplicate conversations are skipped, and existing native
transcripts are not overwritten. Before writing anything, generated transcripts are validated
against msync's strict writer schema for the destination client and read back to verify their
conversation identity and message content. Each history directory is locked while its sync plan is
committed, and files are created with no-clobber semantics so a concurrent user or client write wins
safely.

Generated histories contain a `.msync-manifest.json` sidecar. It records hashes and msync's logical
session identity without adding private fields to Claude or Codex records, keeping the generated
JSONL within the native client schema. Do not delete the manifest if you want later uploads and
cross-provider syncs to retain the same session identity.

Use another server explicitly with `--url`; regular use should rely on `MSYNC_ENDPOINT` so secrets
and server addresses do not need to be repeated:

```console
$ msync sync --dir ~/.codex --url https://history.example.com
$ msync search "database migration" --url https://history.example.com
```

## Server configuration

Each entry in `MSYNC_SERVER_ACCOUNTS` uses `username,password[,access-token]`. Separate multiple
accounts with semicolons:

```console
$ export MSYNC_SERVER_ACCOUNTS='alice,alice-password,alice-token;bob,bob-password,bob-token'
```

Usernames and tokens must be unique. Commas and semicolons cannot appear inside these values.

To use an existing PostgreSQL database instead of the bundled one:

```console
$ export MSYNC_SERVER_ACCOUNTS='alice,web-password,access-token'
$ export MSYNC_DATABASE_URL='postgresql+psycopg://msync:secret@database.example.com/msync'
$ docker compose -f docker/docker-compose.external-db.yaml up -d
```

Set `MSYNC_PORT` to change the published port or `MSYNC_IMAGE` to pin a different image tag. When
PostgreSQL runs on the Docker host, use `host.docker.internal` as its hostname.

The server checks the archive schema at startup and offers to upgrade older schemas. For a shared
archive, stop other msync processes and perform the upgrade during a maintenance window before
restarting uploads.

To run the web UI locally without Docker:

```console
$ MSYNC_SERVER_PASSWORD='choose-a-password' msync server
```

The server uses `~/.msync/msync.sqlite` by default. Set `MSYNC_DATABASE_URL` to a different SQLite
path or SQLAlchemy PostgreSQL URL; database configuration is not accepted as a command-line option.

Open <http://127.0.0.1:8000> and sign in with username `msync`. Set a different username with
`MSYNC_SERVER_USERNAME` or `--username`.

## How data is handled

- SQLite is used by default; PostgreSQL is supported through the same SQLAlchemy schema.
- Source JSONL is retained byte-for-byte in the archive and indexed for search and export.
- Sync and upload are idempotent and use hashes to skip unchanged or duplicate sessions.
- Shared-server data is isolated by account.
- `upload` only reads history directories; `sync` intentionally adds native transcript files to
  every directory passed with `--dir`.

Provider-specific tool calls, reasoning, usage, and system metadata remain in the archive. When a
conversation is converted for the other client, only visible user and assistant messages are
translated into that client's native format.

## Development

```console
$ uv sync --all-extras
$ uv run pytest
$ uv run ruff check .
$ uv run ruff format --check .
```

Install the local checkout as a command with `uv tool install .`.

Licensed under the [Apache License 2.0](LICENSE).
