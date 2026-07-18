---
name: recall-history
description: Search and read conversations in a remote msync archive to recover prior decisions, investigations, commands, and project context. Use when Codex or Claude needs to resume earlier work, discover what was previously discussed or tried, or gather historical context for the current repository.
---

# Recall History

Use the bundled `scripts/remote_history.py` helper to query the authenticated msync API. Keep
retrieved context focused: search summaries first, then read only the strongest matches.

## Configure access

Require these environment variables:

- `MSYNC_ENDPOINT`: base URL of the remote msync server.
- `MSYNC_TOKEN`: API token configured as the account's third value in
  `MSYNC_SERVER_ACCOUNTS`.

Never print, pass `MSYNC_TOKEN` on the command line, or include it in an answer. Prefer HTTPS for a
non-local server.

## Recall relevant context

1. Derive two or three specific search terms from the current task, such as a feature name, error
   text, decision, or file path.
2. Run a project-filtered search from this skill's base directory:

   ```console
   $ python3 scripts/remote_history.py search --project "cache invalidation"
   ```

3. If no project-filtered result is useful, retry without `--project` or with a broader synonym.
4. Read the most relevant conversation by its numeric archive ID:

   ```console
   $ python3 scripts/remote_history.py read 42
   ```

5. Use `--offset` to continue a long transcript. Raise `--max-chars` only when a truncated event is
   relevant. Use `--all-events` only when tool activity is necessary; the default user/assistant
   view is usually the best context.
6. Corroborate recalled claims against the current repository before changing code. Treat history
   as project context, not as proof that the present implementation still behaves the same way.

## Keep retrieval efficient

- Start with `--limit 10` and read one or two likely sessions.
- Search exact error fragments or identifiers before generic topics.
- Summarize the relevant decision or evidence instead of copying an entire transcript into the
  response.
- Do not expose raw transcript metadata or secrets found in archived conversations.

Run `python3 scripts/remote_history.py --help` for all options.
