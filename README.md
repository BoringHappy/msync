# msync

Keep your AI conversations, context, and decisions in sync. `msync` archives local
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) and Codex JSONL transcripts in one
database without changing the source directories. SQLAlchemy provides SQLite, PostgreSQL, and
MySQL persistence through the same schema and upload flow.

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

SQLite support is included. Install the matching driver extra for a server database:

```console
$ uv tool install '.[postgres]'
$ uv tool install '.[mysql]'
```

Python 3.14 or newer is required.

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

The provider is detected from the directory layout and JSONL records. It can be specified when a
custom layout is ambiguous, and the database can be overridden for testing or backups:

```console
$ msync upload --dir /mnt/history --provider codex --database ./history.sqlite
```

`--database` also accepts a SQLAlchemy URL. Common PostgreSQL and MySQL URLs automatically select
the bundled optional Psycopg and PyMySQL drivers:

```console
$ msync upload --dir ~/.codex --database 'postgresql://msync:secret@localhost/msync'
$ msync upload --dir ~/.claude --database 'mysql://msync:secret@localhost/msync'
```

An explicitly selected SQLAlchemy driver works too, such as `postgresql+psycopg://...` or
`mysql+pymysql://...`. Passwords are masked in command output. The target database must already
exist; `msync` creates and versions its tables automatically.

On every connection, `msync` detects whether its schema is absent, initializes a new database from
the SQLAlchemy declarative models, and then validates required tables, columns, primary keys,
unique indexes, and foreign keys. SQLite additionally validates its FTS5 table and synchronization
triggers. A partial, older, or incompatible schema fails before any transcript is uploaded; create
a new database instead of migrating it.

Uploads are idempotent. Each file is addressed by its source location and relative path, then
compared by SHA-256. New files are inserted, changed files replace their normalized event records,
and unchanged files are skipped.

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

## Storage model

The database is deliberately split into distinct storage and indexing layers:

| Table | Purpose |
| --- | --- |
| `schema_info` | Portable application schema version used by SQLAlchemy-managed databases. |
| `locations` | One Claude/Codex data directory, allowing multiple installations of either provider. |
| `conversations` | Session metadata and a zlib-compressed, byte-exact copy of the source JSONL. |
| `events` | Every JSONL record in source order, including its untouched JSON and normalized role/type fields. |
| `message_parts` | Structured content blocks such as text, tool use, and tool results. |
| `events_fts` | SQLite-only FTS5 index maintained automatically for future full-text search. |

The original transcript blob and per-event raw JSON retain all data needed for future export or
conversation reconstruction. Normalized columns are an index, not a replacement for the source.
Foreign keys tie records to the location they came from, so identical session IDs in `.codex` and
`.codex_another` remain independent. Full-length text and binary column variants keep large events
and transcripts safe on MySQL, while SHA-256 path identities avoid backend-specific index-length
limits. PostgreSQL/MySQL full-text indexes can be added as search adapters without changing the
portable archive records.

## Provider architecture

History sources are parallel adapters under `src/msync/providers/`:

| Module | Responsibility |
| --- | --- |
| `base.py` | Shared `HistoryProvider` contract, lossless JSONL reader, and content helpers. |
| `claude.py` | Claude Code discovery, event parsing, session metadata, and subagent handling. |
| `codex.py` | Codex session discovery, event parsing, and session metadata. |
| `__init__.py` | Ordered provider registry, explicit lookup, and automatic format detection. |

The CLI and database use provider names from the registry rather than a hard-coded enum. Adding a
future source requires a `HistoryProvider` subclass and one registry entry; upload orchestration,
SQLAlchemy storage, location isolation, and CLI provider selection remain unchanged.

Automatic detection is deterministic. It first checks whether the directory basename contains a
registered provider name, case-insensitively. Without a name match, each adapter parses only its
fixed internal candidates: Claude checks `history.jsonl` and `projects/**/*.jsonl`; Codex checks
`history.jsonl`, `sessions/**/*.jsonl`, and `archived_sessions/**/*.jsonl`. Claude records are
identified by fields such as `sessionId`/`uuid`, while Codex records use `session_id` or Codex event
types such as `session_meta`. Conflicting evidence is reported as ambiguous instead of guessed.

`msync` only reads the source directory. The archive contains the full conversation content, so it
should be protected like the original `~/.claude` and `~/.codex` directories.

## Development

```console
$ uv sync --all-extras
$ uv run pytest
$ uv run ruff check .
$ uv run ruff format --check .
```
