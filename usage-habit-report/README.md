# usage-habit-report

A local store of token usage from AI coding harnesses. `scan` reads the
harness logs into SQLite and `query` reports on them. Claude Code, Codex
and Antigravity are supported.

No prompt or response text is stored. The one exception is the session title:
the first line (up to 120 characters) of a session's opening prompt.

## Build

```sh
cargo build --release
./target/release/usage-habit-report --help
```

## Locations

| What   | Path                                          |
|--------|-----------------------------------------------|
| Store  | `~/.local/share/usage-habit-report/usage.db`  |
| Config | `~/.config/usage-habit-report/`               |

`XDG_DATA_HOME` and `XDG_CONFIG_HOME` override the parent directories.
`--db <path>` (or `USAGE_HABIT_REPORT_DB`) points every command at a
different store.

## Commands

### `scan`

```sh
usage-habit-report scan [--claude-dir ~/.claude/projects] [--codex-dir ~/.codex] [--antigravity-dir DIR]...
```

Reads every log file from the byte offset where the previous scan stopped,
up to the last complete line, in one transaction per file. A partial last
line is left for the next scan. A file smaller than its stored offset is
re-read from byte 0 after its old rows are dropped. Output:

```
claude-code: 120 files seen, 120 files read, 250000000 bytes read, 5000 rows added, 120 sessions added
codex: 900 files seen, 900 files read, 3000000000 bytes read, 40000 rows added, 890 sessions added, 300 repeated token_count events skipped
antigravity: 48 databases seen, 48 databases read, 175000000 bytes read, 8800 rows added, 48 sessions added
elapsed: 4.50 s
```

Each tool line ends with counts of anything unusual it skipped or corrected;
the Codex ones are explained under [Codex ingest](#codex-ingest).

A second scan over unchanged logs reports `0 files read, 0 bytes read`
(`0 databases read` for Antigravity). Antigravity databases are read whole
when they change; see [Antigravity ingest](#antigravity-ingest).

### `query summary`

```sh
usage-habit-report query summary [--json] [--since 12h|7d|2w]
```

Token totals grouped by tool and model. `--since` keeps requests whose
timestamp falls within the last N hours (`h`), days (`d`), or weeks (`w`).
Without `--json` it prints a table.

JSON shape:

```json
{
  "generated_at": "2026-10-20T04:10:00Z",
  "since": "2026-10-13T04:10:00Z",
  "totals": {
    "requests": 5000,
    "input_tokens": 25000,
    "cache_read_input_tokens": 480000000,
    "cache_creation_input_tokens": 19000000,
    "output_tokens": 4900000
  },
  "groups": [
    {
      "tool": "claude-code",
      "model": "claude-example-model",
      "requests": 3600,
      "input_tokens": 7500,
      "cache_read_input_tokens": 370000000,
      "cache_creation_input_tokens": 12000000,
      "output_tokens": 3000000
    }
  ]
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `generated_at` | string | RFC 3339 UTC time the summary was computed |
| `since` | string or null | RFC 3339 UTC lower bound from `--since`; null means all time |
| `totals` | object | Sum over all groups |
| `groups[]` | array | One entry per (tool, model), ordered by tool, then total tokens descending |
| `tool` | string | `claude-code`, `codex` or `antigravity` |
| `model` | string or null | Raw model name from the log; null when the log has none |
| `requests` | integer | Billed model calls |
| `input_tokens` | integer | Uncached input tokens |
| `cache_read_input_tokens` | integer | Input tokens read from the prompt cache |
| `cache_creation_input_tokens` | integer | Input tokens written to the prompt cache |
| `output_tokens` | integer | Output tokens (includes thinking / reasoning) |

Subagent requests are included in the totals of the model that served them.
To separate them, use `requests.is_subagent` or `sessions.parent_session_id`
in the store (see [Antigravity ingest](#antigravity-ingest) for an example).

## Claude Code ingest

- Source: every `*.jsonl` under `~/.claude/projects`, at any depth.
- `<project>/<uuid>.jsonl` is a session whose id is the file stem.
- `<project>/<parent-uuid>/subagents/agent-<id>.jsonl` is a subagent
  transcript. Its session id is the file stem (`agent-<id>`). It gets
  `is_subagent = 1` and `parent_session_id = <parent-uuid>`, the folder name,
  which matches the `sessionId` field inside its records. `agent_type` comes
  from the sibling `agent-<id>.meta.json`.
- Requests come from `assistant` records with `message.usage`. Claude Code
  writes one record per content block and repeats the usage each time, with
  the output count growing during streaming. Rows are therefore keyed by
  `message.id`, and repeated records merge by taking the maximum of each count.
- Assistant records with model `<synthetic>` are skipped. Claude Code
  generates these locally (errors, interrupts); they are not API calls.
- User turns and the title skip tool results, `isMeta` and compact-summary
  records, and content that starts with `<local-command`, `<command-name`,
  `<command-message`, or `<task-notification`.
- `cwd` is the first non-empty `cwd` field seen. Start and end times are the
  earliest and latest record timestamps.
- Lines that fail to parse are counted and reported by `scan`, then skipped.

## Codex ingest

- Source: every `rollout-*.jsonl` and `rollout-*.jsonl.zst` under
  `~/.codex/sessions/` (dated `YYYY/MM/DD/` folders) and
  `~/.codex/archived_sessions/` (flat). `--codex-dir` (or `CODEX_HOME`) points
  at a different Codex home. Codex zstd-compresses older rollouts; for those,
  offsets and `bytes read` count decompressed bytes. An unchanged `.zst`
  (same size and mtime) is skipped without being decompressed.
- A rollout that is compressed in place or moved to `archived_sessions/` is
  recognised by its file name minus the `.jsonl[.zst]` suffix. The new path
  takes over the old source row and its offset, so nothing is counted twice.
  If two copies are on disk at once, the tracked one (else the plain one) is
  read and the other is skipped.
- Session id is `session_meta.id` from the file's first record. A long session
  can span several files (`rollout-<time>-<id>_<window>.jsonl`); they all
  attach to session `<id>`. `cwd` comes from `session_meta.cwd`.
- Model and effort come from the latest `turn_context` record (`model`,
  `effort`). `session_meta` carries no model. Requests before any
  `turn_context` get a null model; no default is filled in.
- Requests, newer format: one per `token_usage_record` (one per model
  response), keyed by its `response_id`. Older rollouts lack these records;
  then requests come from `event_msg` / `token_count` records, keyed
  `<file stem>#<ordinal>`. Once a file has a `token_usage_record`, its later
  `token_count` records only update the running total.
- `token_count` carries the cumulative `total_token_usage` and the latest
  call's `last_token_usage`. A request is recorded each time the cumulative
  total changes, and its counts are `last_token_usage`. On real data the two
  agree on 46,651 of 46,665 changes. They disagree when the counter restarts
  (it goes down) or jumps forward (seen after a user fork). There the
  cumulative delta is wrong and `last_token_usage` is right. A `token_count` whose total
  did not change repeats the previous one and is skipped. The newer
  per-response records are preferred because about 0.4% of responses get a
  `token_count` whose total never moved (`last_token_usage` all zero); the
  `token_count` path loses those.
- Token columns: Codex's `input_tokens` includes cached input, so
  `input_tokens` = `input_tokens − cached_input_tokens − cache_write_input_tokens`,
  `cache_read_input_tokens` = `cached_input_tokens`,
  `cache_creation_input_tokens` = `cache_write_input_tokens` (always 0 so
  far), `output_tokens` = `output_tokens`, and `reasoning_tokens` =
  `reasoning_output_tokens` (part of the output, not on top of it). If cached
  exceeds raw input, uncached input is clamped to 0 and counted.
- User turns are `response_item` messages with `role: "user"`, unless every
  text block is harness-injected. Injected blocks start with
  `# AGENTS.md instructions` or one of these tags: `<environment_context>`,
  `<user_instructions>`, `<recommended_plugins>`, `<skill>`,
  `<external_codex_apps_open_page>`, `<turn_aborted>`,
  `<send_user_message_question_reply>`, `<codex_delegation>`,
  `<realtime_delegation>`, `<heartbeat>`, `<in-app-browser-context>`,
  `<subagent_notification>`, `<codex_internal_context>`,
  `<guardian_tool_descriptions>`, `<user_shell_command>`, `<image>`.
  Sessions driven only by delegation (voice, or threads an agent created)
  therefore have 0 turns and no title.
- Title: first line of the first typed block. Codex Desktop wraps prompts in
  IDE context (`# Files mentioned by the user`, `# Files pasted by ...`,
  `# Selected text`); the title is the text after the `## My request...` line,
  and none if that is empty.
- Subagents: `session_meta.parent_thread_id` is the parent session id, and
  `source.subagent` the kind. `is_subagent = 1` when either is present (or
  `thread_source` is `subagent`). `agent_type` is `guardian` for auto-review
  subagents (`{"other": "guardian"}`), and `thread_spawn` for spawned agents
  (their `agent_role` when Codex records one; so far always null).
  `session_meta.session_id` is the root session; it is not stored.
- Forks (`forked_from_id` set, which includes most spawned subagents) begin
  with a copy of the parent's history, including its `token_count` records.
  Records with `ordinal` below `subagent_history_start_ordinal` are that copy;
  for forks without that field, records within 1 s of the first record are.
  The copy sets the running total and model, and adds no requests or turns.
  A user fork (no parent) is a top-level session with null
  `parent_session_id`.
- Parser state between scans (running total, model, effort, fork cut) is kept
  per source in `codex_cursors`.

## Antigravity ingest

- Sources: every `*.db` in `~/.gemini/antigravity/conversations` (native
  app) and in `~/.t3/userdata/providers/antigravity/*/antigravity-acp/conversations`
  (T3 bridge). `--antigravity-dir` (repeatable) replaces both. The session id
  is the file stem, which is the conversation id.
- Each database is opened read-only (`mode=ro`). If the WAL/SHM sidecars
  block that, it is opened with `immutable=1`, which ignores the WAL, and
  `scan` reports the count. Nothing is ever written to these databases.
- Requests are generation steps (`step_type` 15) that carry a usage message
  (metadata field 9). The key is `<conversation-id>:<step idx>`. Field 9.2 is
  `input_tokens` (uncached), 9.5 is `cache_read_input_tokens`, 9.3 is
  `output_tokens` (includes thinking), and 9.9 is `reasoning_tokens`.
  Antigravity reports no cache writes, so `cache_creation_input_tokens` is 0.
  The timestamp is the generation start (field 6). Generation steps without
  usage (cancelled or still running) are skipped and picked up once they
  finish.
- Model: the `gen_metadata` row for the same generation, matched by bot id
  (native) or by identical usage bytes (T3), gives the model name (for
  example `gemini-3.8-flash`). When the step's selected model (field 24.8,
  for example `gemini-3.8-flash-high`) extends that name, the suffix is stored
  as `effort`. Without a matching row the selected model is used. Both count
  as `measured`. A generation that names no model gets the database's first
  known model and the session's `model_source` becomes `guessed`. With no
  model anywhere, `model` and `model_source` are null.
- Teamwork child conversations are separate databases. The parent id comes
  from `parent_references`, else field 5 of `trajectory_metadata_blob`. The
  child gets `is_subagent = 1`, `parent_session_id`, and `agent_type` (the
  agent name, field 4.1). Each generation lives in exactly one database, so
  nothing is counted twice. Parent versus child totals for a run:

  ```sql
  SELECT r.is_subagent, SUM(r.output_tokens)
  FROM requests r JOIN sessions s USING (tool, session_id)
  WHERE r.tool = 'antigravity'
    AND (s.session_id = :run OR s.parent_session_id = :run)
  GROUP BY r.is_subagent;
  ```

- Title: the first non-empty line (up to 120 characters) of the first user
  prompt (`step_type` 14). `cwd` comes from the T3 bridge's `<id>.meta`
  (`cwd` or `allowed_tools_workspace`), else the native workspace URI in
  `trajectory_metadata_blob` field 7.
- Incremental: the source row stores the combined size and latest mtime of
  the database and its `-wal` file. When both match, the database is not
  opened. Otherwise every step is read again, because SQLite rewrites rows in
  place; requests merge by key. `offset` holds the next unseen step idx, so
  only prompts at or past it add to `user_turns`. A database whose steps fall
  below `offset` is reset and re-read.

## Store schema (v2)

The schema version is kept in `PRAGMA user_version`; migrations live in
`src/store.rs`.

- `sources`: one row per log file, with `path`, `offset` (bytes consumed;
  for Antigravity, the next unseen step idx), `size`, `mtime_ms`, and
  `scanned_at`.
- `sessions`: one row per `(tool, session_id)`, with `is_subagent`,
  `parent_session_id`, `agent_type`, `title`, `cwd`, `started_at_ms`,
  `ended_at_ms`, `user_turns`, and `model_source` (Antigravity:
  `measured` or `guessed`). The nullable columns `project`,
  `t3_thread_id`, `t3_title`, `label`, and `label_source` are reserved for
  later tickets.
- `requests`: one row per `(tool, request_key)`, with `session_id`,
  `is_subagent`, `source_id`, `ts_ms`, `model`, `effort`, and the four token
  counts. `reasoning_tokens` is filled where the harness reports it (Codex,
  Antigravity; null for Claude Code). The nullable columns `canonical_model`
  and `cost_usd` are reserved for later tickets.
- `codex_cursors` (v2): one row per Codex source, `state` = JSON parser state
  carried between incremental scans.

## Tests

```sh
cargo test
```

The fixtures in `tests/fixtures/` are hand-written. The Antigravity
databases and their protobuf blobs are built inside `tests/antigravity.rs`. Never commit real
transcript content: this repository is public.
