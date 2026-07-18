# msync

Keep your AI conversations, context, and decisions in sync. `msync` archives local
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) and Codex JSONL transcripts in one
database, then can generate native histories that both clients recognize and resume. SQLAlchemy
provides SQLite and PostgreSQL persistence through the same schema and sync flow.

## Install

Install or upgrade the CLI directly from GitHub with [uv](https://docs.astral.sh/uv/):

```console
$ uv tool install --upgrade git+https://github.com/BoringHappy/msync.git
```

For local development:

```console
$ uv sync
$ uv run msync --help
```

To make the command available outside the checkout:

```console
$ uv tool install .
```

Python 3.14 or newer is required.

## Docker

The published container image is available at `ghcr.io/boringhappy/msync`. To run msync with the
bundled PostgreSQL service, configure the accounts and database password, then start the default
Compose configuration:

```console
$ export MSYNC_SERVER_ACCOUNTS='alice,alice-web-password,alice-upload-token;bob,bob-web-password,bob-upload-token'
$ export POSTGRES_PASSWORD='choose-a-strong-database-password'
$ make docker-up-postgres
```

PostgreSQL data is retained in a named volume. The container safely encodes the database URL at
startup, so the PostgreSQL password can contain URL-reserved characters without additional
escaping.

To connect only the msync container to an existing PostgreSQL database, use the external-database
configuration and provide its SQLAlchemy URL:

```console
$ export MSYNC_SERVER_ACCOUNTS='alice,alice-web-password,alice-upload-token;bob,bob-web-password,bob-upload-token'
$ export MSYNC_DATABASE_URL='postgresql+psycopg://msync:secret@database.example.com/msync'
$ make docker-up-external-db
```

Use `host.docker.internal` as the database host when the database runs on the Docker host. Both
configurations expose the browser on `http://localhost:8000` and use
`ghcr.io/boringhappy/msync:latest`. `MSYNC_SERVER_ACCOUNTS` contains semicolon-separated
`username,password[,token]` entries. Each password authenticates that user's browser login; the
optional, unique token authenticates that user's remote uploads. Commas and semicolons cannot be
used inside these credentials. Override `MSYNC_PORT` or `MSYNC_IMAGE` when a different port or
pinned image tag is needed.

## Upload history

Archive Codex history into the default `~/.msync/msync.sqlite` database:

```console
$ msync upload --dir ~/.codex
```

Claude Code and additional installations work the same way:

```console
$ msync upload --dir ~/.claude
$ msync upload --dir ~/.codex_another
```

Locations are identified by account, hostname, and directory path, so users or machines that use
the same path remain distinct in a shared archive. Direct database uploads use the legacy local
account. Both `upload` and `sync` default to the local machine name; override it with `--hostname`
or `MSYNC_HOSTNAME` when a stable alias is preferable:

```console
$ msync upload --dir ~/.codex --hostname workstation-a
```

The provider is detected from the directory layout and JSONL records. It can be specified when a
custom layout is ambiguous, and the database can be overridden for testing or backups:

```console
$ msync upload --dir /mnt/history --provider codex --database ./history.sqlite
```

`--database` also accepts a SQLAlchemy URL. PostgreSQL URLs automatically select Psycopg, which is
included by default:

```console
$ msync upload --dir ~/.codex --database 'postgresql://msync:secret@localhost/msync'
```

An explicitly selected SQLAlchemy driver works too, such as `postgresql+psycopg://...`. Passwords
are masked in command output. The target database must already exist; `msync` creates and versions
its tables automatically. Other database backends, including MySQL, are rejected.

To upload through a running msync server instead of connecting directly to its database, pass the
server's base URL and an account upload token:

```console
$ msync upload --dir ~/.codex --url https://history.example.com --token "$MSYNC_UPLOAD_TOKEN"
```

`MSYNC_UPLOAD_TOKEN` can supply the token without putting it in shell history. `--url` and
`--database` are mutually exclusive. The client detects and reads native transcripts locally,
streams their byte-exact contents one file at a time over the authenticated API, and records the
client's hostname and source path on the server. A history directory has no aggregate upload limit;
each individual transcript is capped at 256 MiB. The server rejects oversized request bodies before
parsing them and spools accepted network bodies to disk before archive processing. Remote uploads
have the same hash-based update, duplicate, and schema checks as direct database uploads. Treat an
upload token like a password and use HTTPS whenever the server is reached over a network.

On every connection, `msync` detects whether its schema is absent, initializes a new database from
the SQLAlchemy declarative models, and then validates required tables, columns, primary keys,
unique indexes, and foreign keys. SQLite additionally validates its FTS5 table and synchronization
triggers. Existing schemas are migrated by the explicit `msync upgrade` maintenance command.
During the development phase, version 5 is the oldest supported upgrade baseline; earlier
development schemas should be recreated or exported with their compatible msync release. Known
migrations are registered as sequential steps. Schema v7 adds account ownership and tenant-local
revision uniqueness. Future migrations can extend the same chain without changing the CLI
workflow. Partial or otherwise incompatible schemas fail before any transcript is uploaded.

For a shared or large archive, stop uploads and run the schema upgrade explicitly before starting
the web server:

```console
$ msync upgrade --database 'postgresql://msync:secret@localhost/msync'
```

The command reports each migration step and reindexing progress, and waits at most 10 seconds for
concurrent database transactions by default. Use `--lock-timeout SECONDS` to change that limit. If
another upload still holds a lock, the upgrade exits with recovery instructions instead of
appearing to hang. Regular commands never perform a long migration during startup; they direct the
operator to `msync upgrade` when the archive is behind. Upgrade every machine using a shared
archive before resuming uploads.

Uploads are idempotent. Each file is addressed by its source location and relative path, then
compared by SHA-256. New files are inserted, changed files replace their normalized event records,
and unchanged files are skipped. Before insertion, msync also skips a logical session revision that
is already archived through another location or provider.

The raw SHA-256 identifies an exact provider transcript. A canonical chat SHA-256 hashes the
ordered visible `(role, text)` turns, while a stable logical session UUID follows the conversation
through provider conversions and location changes. Both have indexed columns on `conversations`
and are also stored under `metadata_json._msync`. A database unique index on
`(account_username, logical_session_id, chat_sha256)` prevents concurrent or sequential uploads of
an exported Claude or Codex copy from creating another conversation row within the same account. A
changed chat hash distinguishes a new revision of the same logical session.

`upload` only reads the source directory. Use `sync` when native history files should also be
written.

## Sync Claude and Codex

Merge both local histories through the archive and write each conversation back in the native
format of both clients:

```console
$ msync sync --dir ~/.claude --dir ~/.codex
```

The command runs in two phases: it first archives new or changed native transcripts from every
location, then writes all archived conversations into each location's provider format. Session
boundaries remain intact, so each source conversation appears as a separate resumable conversation
instead of one combined transcript. Repeating the command is idempotent.

Provider names are normally detected from the directory name or content. Specify one provider per
directory, in the same order, for neutral or newly created locations:

```console
$ msync sync --dir /mnt/merged-claude --provider claude
$ msync sync --dir /mnt/a --dir /mnt/b --provider claude --provider codex
```

`sync` uses the same `--database` option as the other commands, including PostgreSQL URLs. This also
makes it possible to generate a new native history location from conversations that were uploaded
earlier:

```console
$ msync sync --dir /mnt/merged-codex --provider codex --database ./history.sqlite
```

Native transcripts already belonging to the target provider are copied byte-for-byte when needed.
Cross-provider conversion writes the visible user and assistant messages using typed native JSONL
schemas. Provider-specific tool calls, reasoning, usage, and system metadata remain losslessly
available in the archive but are not translated into the other provider's execution protocol.

Every destination contains a `.msync-manifest.json` provenance file. It prevents exported copies
from feeding back into the archive on the next sync. Existing sessions are immutable: a changed
source receives a new deterministic revision ID and path, and same-provider path collisions are
cloned under that revision identity instead of overwriting either session. Sessions continued in
Claude or Codex are left untouched. Revision IDs derive only from the stable logical session UUID
and canonical chat SHA-256, so the same revision gets the same native ID regardless of its source
provider or location. A session already native to the destination location is skipped. Unknown path
collisions are reported and left untouched. Sync rejects symlinks in every destination path
component, and generated transcript and manifest files use owner-only permissions.

## Search history

Search the normalized message text in the default archive:

```console
$ msync search "blue widget"
```

Search uses a portable SQL `LIKE` query and returns each matching event with its provider,
conversation, timestamp, role, and message text. Pass `--database` (or `--db`) to search a different
archive:

```console
$ msync search "blue widget" --database ./history.sqlite
```

Randomly inspect a limited number of non-empty archived messages to spot-check imported data:

```console
$ msync sample 5
$ msync sample 5 --database ./history.sqlite
```

## Browse history on the web

Start the authenticated FastAPI history browser against the default archive:

```console
$ MSYNC_SERVER_PASSWORD='choose-a-strong-password' msync server
```

Then open `http://127.0.0.1:8000` and sign in as `msync`. The web UI uses a terminal-inspired
Claude/Codex layout with a location picker, session search, chronological message rendering, and
lossless event inspection. Tool calls and results share compact activity cards, while assistant
messages render common Markdown, GitHub-style tables, and fenced code blocks without accepting
embedded HTML. Injected Claude skill/context records are kept out of the human conversation and
remain available in **Raw events**. Order the session list by time, activity, or title. Long
conversation titles stay on one line and expose their full value on hover or keyboard focus. Use the
**Activity**, **Chat**, **Tools**, and **Reasoning** filters (or keys 1–4), navigate messages with
J/K and sessions with `[`/`]`, or jump between human messages with the floating arrows (Alt+↑/↓).
Find text within the currently loaded events from the toolbar. **Fit width** switches between the
focused reading column and the full window and remembers the selection. Large session lists and
long transcripts load incrementally. The header refreshes archive contents, **Copy link** creates a
deep link to the active session, and the mobile session drawer closes by tapping outside it.
Select **Raw events** (or press Ctrl+O) to inspect every native record and its source JSON.

If startup detects an old schema, it asks whether to upgrade the database. Answer `y` to run the
registered migrations with the same bounded lock wait and progress reporting as `msync upgrade`,
or accept the default `N` to leave the database unchanged and stop the server. For unattended
startup, run `msync upgrade` explicitly during a maintenance window before launching the server.

Choose a different archive, login, address, or port with command options:

```console
$ MSYNC_SERVER_PASSWORD='secret' msync server \
    --database ./history.sqlite --username reader --host 127.0.0.1 --port 8765
```

`MSYNC_SERVER_USERNAME` can also set the username. If the password environment variable is absent,
the command prompts without echoing the password. The default loopback address keeps the browser
local. HTTP Basic credentials are not encrypted in transit, so put msync behind an HTTPS reverse
proxy before binding it to a network-accessible address.

For a shared server, configure multiple accounts in `username,password[,token]` format, separated
by semicolons:

```console
$ MSYNC_SERVER_ACCOUNTS='alice,alice-web-password,alice-upload-token;bob,bob-web-password' \
    msync server --database ./history.sqlite --host 0.0.0.0
```

The same value can be passed with `--accounts`. Commas and semicolons are delimiters and therefore
cannot appear inside usernames, passwords, or tokens; usernames and tokens must also be unique.
Every account can sign in to the browser with HTTP Basic authentication. The optional third field
enables Bearer-token uploads for that account. An entry with only username and password, such as
`bob` above, is browser-only and cannot upload remotely. The username is also the persistent tenant
identifier in the archive, so keep it stable; passwords and tokens can be rotated independently.

Locations, conversations, duplicate detection, list results, and conversation-detail lookups are
isolated by account. A user cannot discover or fetch another user's conversation by guessing its
numeric ID. After upgrading an existing single-user archive, its legacy unowned records are visible
only to the first configured account; new token uploads are owned directly by the token's account.
The original `MSYNC_SERVER_USERNAME`/`MSYNC_SERVER_PASSWORD` and `--username`/`--password` options
remain available for single-user browsing, but that compatibility account has no upload token.

## Storage model

The database is deliberately split into distinct storage and indexing layers:

| Table | Purpose |
| --- | --- |
| `schema_info` | Portable application schema version used by SQLAlchemy-managed databases. |
| `locations` | One account, hostname, and Claude/Codex data-directory tuple, allowing isolated users, machines, and installations. |
| `conversations` | Account-owned session metadata, tenant-local logical revision identity, and a zlib-compressed byte-exact source JSONL. |
| `events` | Every JSONL record in source order, including its untouched JSON and normalized role/type fields. |
| `message_parts` | Structured content blocks such as text, tool use, and tool results. |
| `events_fts` | SQLite-only FTS5 index maintained automatically for future full-text search. |

The retained transcript blob and per-event raw JSON preserve all data needed for future export or
conversation reconstruction. Normalized columns are an index, not a replacement for the retained
source. Schema upgrades can therefore rebuild normalized events from the byte-exact transcript;
schema v6 does this once to distinguish human messages from tool content in existing archives.
Foreign keys record the location of the retained copy. Identical logical revisions found in
another provider or location are skipped; different revisions remain distinct when they exist as
separate source transcripts. SHA-256 hostname/path identities keep portable indexes compact.
PostgreSQL full-text indexes can be added as a search adapter without changing the portable archive
records.

## Provider architecture

History sources are parallel adapters under `src/msync/providers/`:

| Module | Responsibility |
| --- | --- |
| `base.py` | Shared `HistoryProvider` contract, lossless JSONL reader, and content helpers. |
| `claude.py` | Claude Code discovery, event parsing, session metadata, and subagent handling. |
| `codex.py` | Codex session discovery, event parsing, and session metadata. |
| `__init__.py` | Ordered provider registry, explicit lookup, and automatic format detection. |

Native message and rollout contracts are Pydantic models under `src/msync/schemas/`. They validate
the fields msync reads and writes while retaining unknown fields added by future client versions.
The Claude and Codex adapters use these models for both import validation and native generation.

The CLI and database use provider names from the registry rather than a hard-coded enum. Adding a
future source requires a `HistoryProvider` subclass and one registry entry; upload orchestration,
SQLAlchemy storage, location isolation, and CLI provider selection remain unchanged.

Automatic detection is deterministic. It first checks whether the directory basename contains a
registered provider name, case-insensitively. Without a name match, each adapter parses only its
fixed internal candidates: Claude checks `history.jsonl` and `projects/**/*.jsonl`; Codex checks
`history.jsonl`, `sessions/**/*.jsonl`, and `archived_sessions/**/*.jsonl`. Claude records are
identified by fields such as `sessionId`/`uuid`, while Codex records use `session_id` or Codex event
types such as `session_meta`. Conflicting evidence is reported as ambiguous instead of guessed.

The archive contains the full conversation content, so it should be protected like the original
`~/.claude` and `~/.codex` directories. `upload` is read-only for provider directories; `sync`
intentionally creates native transcripts in every directory passed to it.

## Development

```console
$ uv sync --all-extras
$ uv run pytest
$ uv run ruff check .
$ uv run ruff format --check .
```
