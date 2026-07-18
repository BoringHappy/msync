# msync

Keep your Claude Code and Codex conversations in one searchable archive.

`msync` can copy conversations between both clients, back them up to your own server, and provide a
private web UI for browsing them later. Original transcript data is preserved.

## Choose what you need

| I want to... | Start with |
| --- | --- |
| Keep Claude Code and Codex history in sync | `msync sync --dir ~/.claude --dir ~/.codex` |
| Search my local archive | `msync search "search text"` |
| Browse my local archive | `MSYNC_SERVER_PASSWORD='password' msync server` |
| Back up history to another machine | [Run a server](#back-up-to-an-msync-server), then use `msync upload` |
| Upload every completed session automatically | [Install the plugin](#automatic-uploads) |

## Install

Requirements: Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```console
$ uv tool install --upgrade git+https://github.com/BoringHappy/msync.git
$ msync --help
```

## Sync Claude Code and Codex locally

```console
$ msync sync --dir ~/.claude --dir ~/.codex
```

This command:

1. Archives new or changed conversations in `~/.msync/msync.sqlite`.
2. Adds each conversation to the other client's history in its native format.
3. Keeps sessions separate and resumable.

Running it again is safe: unchanged and duplicate conversations are skipped. Existing native
transcripts are not overwritten.

For a custom directory, specify its provider when it cannot be detected from its name or contents:

```console
$ msync sync --dir /mnt/claude-history --provider claude
```

Use another SQLite file or a PostgreSQL database with `--database`:

```console
$ msync sync --dir ~/.codex --database ./history.sqlite
```

## Search or browse the archive

Search message text from the terminal:

```console
$ msync search "database migration"
$ msync sample 5
```

Start the private web UI:

```console
$ MSYNC_SERVER_PASSWORD='choose-a-password' msync server
```

Open <http://127.0.0.1:8000> and sign in with username `msync` and the password you chose. Set a
different username with `MSYNC_SERVER_USERNAME` or use `--username`.

The browser includes archive summaries, conversation search, message and tool filters, raw event
inspection, and deep links to sessions.

## Back up to an msync server

Remote backup has two parts: run an authenticated server, then upload from each client machine.

### 1. Run the server with Docker

Clone this repository, then start msync with its bundled PostgreSQL database:

```console
$ export MSYNC_SERVER_ACCOUNTS='alice,web-password,access-token'
$ export POSTGRES_PASSWORD='choose-a-strong-database-password'
$ make docker-up-postgres
```

Open <http://localhost:8000> and sign in as `alice` with `web-password`.

Each account uses `username,password[,access-token]`. Separate multiple accounts with semicolons:

```console
$ export MSYNC_SERVER_ACCOUNTS='alice,alice-password,alice-token;bob,bob-password,bob-token'
```

Usernames and tokens must be unique. Commas and semicolons cannot appear inside these values.

To use an existing PostgreSQL database instead:

```console
$ export MSYNC_SERVER_ACCOUNTS='alice,web-password,access-token'
$ export MSYNC_DATABASE_URL='postgresql+psycopg://msync:secret@database.example.com/msync'
$ make docker-up-external-db
```

Set `MSYNC_PORT` to change the published port or `MSYNC_IMAGE` to pin a different image tag. When
PostgreSQL runs on the Docker host, use `host.docker.internal` as its hostname.

### 2. Upload history from a client

```console
$ export MSYNC_ENDPOINT='https://history.example.com'
$ export MSYNC_TOKEN='alice-token'
$ msync upload --dir ~/.claude
$ msync upload --dir ~/.codex
```

The URL can also be passed with `--url` and the token with `--token`. Environment variables are
recommended because they keep the token out of shell history.

`upload` is read-only for the source directory. It verifies transcripts before sending them and
uploads only new or changed sessions. A failed transcript is reported without preventing other
valid transcripts from uploading. Each transcript can be up to 256 MiB.

Use HTTPS whenever the server is available over a network. Access tokens can upload and read the
account's archive, so protect them and the archive contents like the original Claude Code and Codex
history directories.

## Automatic uploads

The included plugin installs or upgrades `msync` when a Claude Code or Codex session starts, then
queues the completed session for upload after every turn. Upload hooks return immediately and safely
ignore repeated events.

Set these variables in the environment that launches your client:

```console
$ export MSYNC_ENDPOINT='https://history.example.com'
$ export MSYNC_TOKEN='alice-token'
```

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

The plugin also includes the `recall-history` skill for both Claude Code and Codex. It searches the
remote archive with the same `MSYNC_TOKEN`, filters sessions to the current repository when asked,
and reads selected conversations as project context. For example, ask the agent to use
`recall-history` to find an earlier decision or investigation. The skill never passes the token on
the command line.

## Useful commands

| Command | Purpose |
| --- | --- |
| `msync sync` | Archive local histories and write native Claude/Codex copies |
| `msync upload` | Send new or changed transcripts to a remote server |
| `msync search` | Find text in archived messages |
| `msync sample` | Inspect random archived messages |
| `msync server` | Start the authenticated web UI and upload API |

Run `msync COMMAND --help` for all options.

### Database upgrades

`msync server` checks the archive schema before startup and offers to upgrade old schemas. For a
shared archive, stop other msync processes and approve the upgrade during a maintenance window
before restarting uploads.

```console
$ msync server --database ~/.msync/msync.sqlite
```

## How data is handled

- SQLite is used by default; PostgreSQL is supported through the same SQLAlchemy schema.
- Source JSONL is retained byte-for-byte in the archive and indexed for search and export.
- Sync and upload are idempotent and use hashes to skip unchanged or duplicate sessions.
- Generated histories include a `.msync-manifest.json` file to prevent exported copies from being
  imported again.
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
