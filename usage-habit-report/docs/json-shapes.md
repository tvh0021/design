# JSON shapes (frozen)

Every `query` command prints this JSON with `--json`. The MCP layer mirrors
these shapes exactly. A field is never renamed, retyped or removed; new
optional fields may be added at the end. Examples in README.md show sample
values; this file lists names and types.

Conventions:

- Timestamps are RFC 3339 UTC strings with second precision (`2026-10-20T04:10:00Z`).
- `T?` means the field is always present and may be `null`.
- Token fields are integers. `estimated_cost_usd` is a USD estimate at LiteLLM
  list prices: `null` when no request in the group has a price, otherwise the
  sum over the priced ones. `unpriced_requests` counts the requests left out.
- `--since 12h|7d|2w` keeps requests at or after now minus the window; the
  echoed `since` field is the resulting lower bound (`null` for all time).
- No prompt or response text appears anywhere. The only free text is a
  session `title` (first line of the opening prompt, 120 characters at most)
  and a T3 thread title.
- Exit codes: 0 found, 1 not found, 2 ambiguous (`query project`,
  `query session`, `query thread`). With `--json` the error body is on stdout;
  a one-line message is on stderr either way.
- Before running, every `query` does an incremental scan, enrich and price
  pass unless `--no-scan` is given. The scan line goes to stderr, so stdout is
  always the JSON alone.

## Shared object: `Tokens`

Flattened into the objects that say "Tokens".

| Field | Type |
|-------|------|
| `requests` | integer |
| `input_tokens` | integer (uncached input) |
| `cache_read_input_tokens` | integer |
| `cache_creation_input_tokens` | integer |
| `output_tokens` | integer |
| `estimated_cost_usd` | number? |
| `unpriced_requests` | integer |

## Error bodies

Not found (exit 1), for `query project`, `query session`, `query thread`:

```json
{"error": "not found", "key": "<the key as given>"}
```

Ambiguous (exit 2):

```json
{"error": "ambiguous", "key": "<key>", "candidates": [ ... ]}
```

`query project` candidates: `{project: string, sessions: integer}`.
`query session` and `query thread` candidates (`SessionCandidate`):

| Field | Type |
|-------|------|
| `tool` | string: `claude-code`, `codex`, `antigravity` |
| `session_id` | string |
| `matched_by` | string: `session_id`, `t3_thread_id`, `session_id_prefix` |
| `title` | string? |
| `project` | string? |
| `t3_thread_id` | string? |
| `started_at` | string? |

## `query session <key>` and `query thread <t3-thread-id>`

`<key>` is matched as an exact session id, then an exact T3 thread id, then a
session-id prefix; the first stage with any match decides. `thread` tries only
the T3 thread id stage. Flags: `--rows`, `--since`, `--json`.

| Field | Type |
|-------|------|
| `generated_at` | string |
| `since` | string? |
| `rates_fetched_at` | string? |
| `matched_by` | string: `session_id`, `t3_thread_id`, `session_id_prefix` |
| `tool` | string |
| `session_id` | string |
| `title` | string? |
| `project` | string? (path) |
| `project_name` | string? |
| `is_subagent` | boolean |
| `parent_session_id` | string? |
| `agent_type` | string? |
| `t3_thread_id` | string? |
| `t3_title` | string? |
| `label` | string?: hand-set if present, else rule label; a subagent reports its root session's |
| `label_source` | string?: `manual`, `rule` |
| `started_at` | string? |
| `ended_at` | string? |
| `elapsed_seconds` | integer? |
| `models` | array of `{model: string?, canonical_model: string?, requests: integer}`, most requests first |
| `usage` | object: Tokens plus `reasoning_tokens: integer?` (null when no request reports it) |
| `subagents` | object? (null for a subagent session), see below |
| `rows` | array? (null unless `--rows`), oldest first, see below |
| `active_seconds` | integer? (whole session and linked descendants, idle gaps removed) |
| `idle_gap_seconds` | integer (900) |

`started_at`, `ended_at`, `elapsed_seconds` and `active_seconds` describe the
session, not the `--since` window. `models`, `usage`, `subagents` and `rows` cover requests in
the window. Active time merges timestamp-only event intervals for the session
and its linked descendants, then removes every gap strictly greater than
`idle_gap_seconds`. One isolated timestamp contributes zero seconds. A missing
timeline reports null until its source is scanned. `usage` counts the session's
own transcript, not its subagents.

`subagents` (same definitions as `query agents`; tokens are uncached input plus output):

| Field | Type |
|-------|------|
| `subagent_count` | integer (nested subagents included) |
| `subagent_models` | array of `{model: string?, requests: integer}` |
| `subagent_efforts` | array of `{effort: string?, requests: integer}` |
| `subagent_tokens` | integer |
| `total_tokens` | integer (session plus subagents) |
| `subagent_token_share` | number? (0 to 1) |
| `max_concurrent_fanout` | integer |

`rows[]`:

| Field | Type |
|-------|------|
| `ts` | string |
| `model` | string? |
| `effort` | string? |
| `input_tokens` | integer |
| `cache_read_input_tokens` | integer |
| `cache_creation_input_tokens` | integer |
| `output_tokens` | integer |
| `reasoning_tokens` | integer? |
| `estimated_cost_usd` | number? |

## `query summary`

| Field | Type |
|-------|------|
| `generated_at` | string |
| `since` | string? |
| `rates_fetched_at` | string |
| `totals` | Tokens |
| `groups[]` | Tokens plus `tool: string`, `model: string?`; ordered by tool, then total tokens descending |

## `query summary --group-by label`

| Field | Type |
|-------|------|
| `generated_at` | string |
| `since` | string? |
| `rates_fetched_at` | string |
| `group_by` | string: `label` |
| `totals` | Tokens |
| `groups[]` | Tokens plus `label: string`, `tool: string` |

## `query labels`

| Field | Type |
|-------|------|
| `generated_at` | string |
| `totals` | Tokens |
| `labels[]` | Tokens plus `label: string`, `sessions: integer`, `manual_sessions: integer`, `subagent_sessions: integer` |

## `query models`

A bare array (no wrapper), unpriced rows first:

| Field | Type |
|-------|------|
| `tool` | string |
| `model` | string? (raw name) |
| `canonical_model` | string? |
| `litellm_key` | string? |
| `priced` | boolean |
| `requests` | integer |

## `query projects`

| Field | Type |
|-------|------|
| `generated_at` | string |
| `since` | string? |
| `rates_fetched_at` | string |
| `projects[]` | Tokens plus `project: string?`, `name: string?`, `sessions: integer`, `first_request_at: string?`, `last_request_at: string?`, `active_seconds: integer`, `elapsed_seconds: integer`, `idle_gap_seconds: integer`; null `project`/`name` is the row for sessions with no known directory |

## `query project <key>`

| Field | Type |
|-------|------|
| `generated_at` | string |
| `since` | string? |
| `rates_fetched_at` | string |
| `project` | string |
| `name` | string |
| `sessions` | integer |
| `active_seconds` | integer (sum of top-level sessions, including unlinked children) |
| `elapsed_seconds` | integer (sum of top-level wall spans) |
| `idle_gap_seconds` | integer (900) |
| `first_request_at` | string? |
| `last_request_at` | string? |
| plus Tokens | for the whole project |
| `tools[]` | Tokens plus `tool: string`, `sessions: integer`, `first_request_at: string?`, `last_request_at: string?`, `active_seconds: integer`, `elapsed_seconds: integer`, `idle_gap_seconds: integer` |

Project time fields sum the whole cached duration of each top-level session
selected by the session window, not only its events in that window. Linked
children are already covered by their parent. An unlinked child counts as a
top-level session. Token and session-count fields retain their existing meaning.

## `query agents`

| Field | Type |
|-------|------|
| `generated_at` | string |
| `since` | string? |
| `coverage` | object keyed by tool: `{supported: boolean, note: string}` |
| `totals` | `{top_level_sessions, sessions_with_subagents, subagent_sessions, unlinked_subagent_sessions}`, all integers |
| `subagent_token_share` | `{subagent_tokens: integer, total_tokens: integer, share: number?}` |
| `histogram[]` | `{bucket: string, min: integer, max: integer?, sessions: integer}` |
| `by_tool[]` | `{tool, top_level_sessions, sessions_with_subagents, subagent_sessions, unlinked_subagent_sessions, subagent_tokens, total_tokens: integer-valued, subagent_token_share: number?}` (`tool` is a string) |
| `model_mix` | `{main_thread: [ModelMixEntry], subagent: [ModelMixEntry]}`; `ModelMixEntry` is `{model: string?, requests: integer, tokens: integer, request_share: number}` |
| `subagent_efforts[]` | `{effort: string?, requests: integer}` |
| `heaviest_fanouts[]` | `{tool: string, session_id: string, title: string?, subagent_count: integer, subagent_models: [{model: string?, requests}], subagent_efforts: [{effort: string?, requests}], subagent_tokens: integer, total_tokens: integer, subagent_token_share: number?, max_concurrent_fanout: integer}`, at most 10 |
