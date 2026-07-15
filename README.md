# msync

Keep your AI conversations, context, and decisions in sync. `msync` archives local
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) and Codex JSONL transcripts in one
database, then can generate native histories that both clients recognize and resume. SQLAlchemy
provides SQLite, PostgreSQL, and MySQL persistence through the same schema and sync flow.

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

SQLite and PostgreSQL support are included. Install the driver extra to use MySQL:

```console
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

Locations are identified by both hostname and directory path, so machines that use the same path
remain distinct in a shared PostgreSQL archive. Both `upload` and `sync` default to the local
machine name; override it with `--hostname` or `MSYNC_HOSTNAME` when a stable alias is preferable:

```console
$ msync upload --dir ~/.codex --hostname workstation-a
```

The provider is detected from the directory layout and JSONL records. It can be specified when a
custom layout is ambiguous, and the database can be overridden for testing or backups:

```console
$ msync upload --dir /mnt/history --provider codex --database ./history.sqlite
```

`--database` also accepts a SQLAlchemy URL. Common PostgreSQL and MySQL URLs automatically select
Psycopg (included by default) and the optional PyMySQL driver:

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
triggers. Schema version 3 is migrated in place by backfilling logical revision identities and
collapsing duplicates before the unique index is created. Version 4 archives are then migrated to
hostname-aware location identities. A legacy location is recorded with hostname `unknown` until
its source uploads again or uses an explicit `--hostname`. Partial, older, or otherwise
incompatible schemas fail before any transcript is uploaded.

Uploads are idempotent. Each file is addressed by its source location and relative path, then
compared by SHA-256. New files are inserted, changed files replace their normalized event records,
and unchanged files are skipped. Before insertion, msync also skips a logical session revision that
is already archived through another location or provider.

The raw SHA-256 identifies an exact provider transcript. A canonical chat SHA-256 hashes the
ordered visible `(role, text)` turns, while a stable logical session UUID follows the conversation
through provider conversions and location changes. Both have indexed columns on `conversations`
and are also stored under `metadata_json._msync`. A database unique index on
`(logical_session_id, chat_sha256)` prevents concurrent or sequential uploads of an exported Claude
or Codex copy from creating another conversation row. A changed chat hash distinguishes a new
revision of the same logical session.

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

`sync` uses the same `--database` option as the other commands, including PostgreSQL and MySQL
URLs. This also makes it possible to generate a new native history location from conversations
that were uploaded earlier:

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
lossless event inspection. Select **Expand details** (or press Ctrl+O) to include metadata/model
events and raw source JSON; each visible message also has its own **details** button.

Choose a different archive, login, address, or port with command options:

```console
$ MSYNC_SERVER_PASSWORD='secret' msync server \
    --database ./history.sqlite --username reader --host 127.0.0.1 --port 8765
```

`MSYNC_SERVER_USERNAME` can also set the username. If the password environment variable is absent,
the command prompts without echoing the password. The default loopback address keeps the browser
local. HTTP Basic credentials are not encrypted in transit, so put msync behind an HTTPS reverse
proxy before binding it to a network-accessible address.

## Storage model

The database is deliberately split into distinct storage and indexing layers:

| Table | Purpose |
| --- | --- |
| `schema_info` | Portable application schema version used by SQLAlchemy-managed databases. |
| `locations` | One hostname and Claude/Codex data-directory pair, allowing multiple machines and installations. |
| `conversations` | Session metadata, unique logical revision identity, and a zlib-compressed byte-exact source JSONL. |
| `events` | Every JSONL record in source order, including its untouched JSON and normalized role/type fields. |
| `message_parts` | Structured content blocks such as text, tool use, and tool results. |
| `events_fts` | SQLite-only FTS5 index maintained automatically for future full-text search. |

The retained transcript blob and per-event raw JSON preserve all data needed for future export or
conversation reconstruction. Normalized columns are an index, not a replacement for the retained
source. Foreign keys record the location of the retained copy. Identical logical revisions found in
another provider or location are skipped; different revisions remain distinct when they exist as
separate source transcripts. Full-length text and binary column variants keep large events and
transcripts safe on MySQL, while SHA-256 hostname/path identities avoid backend-specific
index-length limits.
PostgreSQL/MySQL full-text indexes can be added as search adapters without changing the portable
archive records.

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
