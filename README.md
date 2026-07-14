# msync

Keep your AI conversations, context, and decisions in sync. `msync` archives local
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) and Codex JSONL transcripts in one
SQLite database without changing the source directories.

## Install

This project uses [uv](https://docs.astral.sh/uv/):

```console
$ uv sync
$ uv run msync --help
```

To make the command available outside the checkout:

```console
$ uv tool install .
```

Python 3.11 or newer is supported.

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

Uploads are idempotent. Each file is addressed by its source location and relative path, then
compared by SHA-256. New files are inserted, changed files replace their normalized event records,
and unchanged files are skipped.

## Storage model

The database is deliberately split into distinct storage and indexing layers:

| Table | Purpose |
| --- | --- |
| `locations` | One Claude/Codex data directory, allowing multiple installations of either provider. |
| `conversations` | Session metadata and a zlib-compressed, byte-exact copy of the source JSONL. |
| `events` | Every JSONL record in source order, including its untouched JSON and normalized role/type fields. |
| `message_parts` | Structured content blocks such as text, tool use, and tool results. |
| `events_fts` | An automatically maintained SQLite FTS5 index for future full-text search. |

The original transcript blob and per-event raw JSON retain all data needed for future export or
conversation reconstruction. Normalized columns are an index, not a replacement for the source.
Foreign keys tie records to the location they came from, so identical session IDs in `.codex` and
`.codex_another` remain independent.

`msync` only reads the source directory. The archive contains the full conversation content, so it
should be protected like the original `~/.claude` and `~/.codex` directories.

## Development

```console
$ uv run pytest
$ uv run ruff check .
$ uv run ruff format --check .
```
