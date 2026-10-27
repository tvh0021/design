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
  and existing harness or T3 thread headlines.
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
| `display_title` | string? |
| `title_source` | string? |

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
| `label_source` | string?: `manual`, `rule`, `headline` |
| `started_at` | string? |
| `ended_at` | string? |
| `elapsed_seconds` | integer? |
| `models` | array of `{model: string?, canonical_model: string?, requests: integer}`, most requests first |
| `usage` | object: Tokens plus `reasoning_tokens: integer?` (null when no request reports it) |
| `subagents` | object? (null for a subagent session), see below |
| `rows` | array? (null unless `--rows`), oldest first, see below |
| `active_seconds` | integer? (whole session and linked descendants, idle gaps removed) |
| `idle_gap_seconds` | integer (900) |
| `parent` | object?: `{tool: string, session_id: string, lineage_source: string?}` |
| `display_title` | string? |
| `title_source` | string?: `codex-name`, `codex-title`, `antigravity-summary`, `t3`, `first-line` |

`parent` identifies the parent across tools. `lineage_source` is `t3` or
`timing` for imported T3 delegations, and null for harness-recorded parents.
The existing `parent_session_id` remains present. Native harness parents take
priority. Timing matches require exactly one unclaimed child whose first event
is within 60 seconds after delegation starts, with matching projects when both
are known. Titles never participate.

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
| `subagent_models` | array of `{model: string?, requests: integer}`; `model` is the canonical name where known |
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
| `groups[]` | Tokens plus `tool: string`, `model: string?` (the canonical name where one is known, else the raw name); ordered by tool, then total tokens descending |

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

## `query value`

| Field | Type |
|-------|------|
| `generated_at` | string |
| `min_rise_percent` | number (20) |
| `runs_averaged` | integer (10) |
| `pace_days` | integer (28) |
| `combined` | `{ plans: integer, monthly_usd: number, pace_monthly_cost_usd: number?, pace_multiple: number? }` over the priced plans |
| `tools[]` | one entry per tool, in tool order |
| `tools[].tool` | string |
| `tools[].subscription` | `{ name: string, monthly_usd: number? }` |
| `tools[].limit_source` | `"readings"` (logged), `"live"` (only `limits record`), `"rejections"` or `"none"` |
| `tools[].reference_model` | string? (costliest model of the last `pace_days` days, else of all time) |
| `tools[].first_request`, `tools[].last_request` | string? |
| `tools[].windows[]` | `session`, `weekly`, then one per live pool and window |
| `tools[].windows[].window` | string |
| `tools[].windows[].window_hours` | integer (5 or 168) |
| `tools[].windows[].pool` | string? (`"Fable"`, `"Gemini"`; null for the plan's own window) |
| `tools[].windows[].models` | string[] (the models a pool covers; empty for the plan's window) |
| `tools[].windows[].readings` | integer, limit samples seen for the window, logged and live |
| `tools[].windows[].live_readings` | integer, of `readings`, those from `limits record` |
| `tools[].windows[].first_reading`, `last_reading` | string? |
| `tools[].windows[].latest` | `{ at: string, used_percent: number, resets_at: string? }?`, the newest reading |
| `tools[].windows[].qualifying_runs` | integer, runs rising at least `min_rise_percent` |
| `tools[].windows[].runs[]` | the runs averaged, largest rise first: `start`, `end` (timestamps), `from_percent`, `to_percent`, `rise_percent` (numbers), `readings`, `sessions` (integers), Tokens, `per_window: Capacity` |
| `tools[].windows[].per_window` | Capacity? (mean of `runs[]`; `null` with no runs) |
| `tools[].windows[].spread` | `{ cost_min, cost_median, cost_max: number?, tokens_min, tokens_median, tokens_max: number }?` over `runs[]` |
| `tools[].windows[].drift[]` | `month: "YYYY-MM"`, `runs: integer`, `per_window: Capacity` (mean of every qualifying run that started that month), `main_model: string?`, `main_model_share: number?` (share of the month's run cost) |
| `tools[].windows[].lower_bound` | Capacity? (busiest sliding window; set only when `per_window` is null and requests exist) |
| `tools[].windows[].utilization` | `{ windows, exhausted, cut_short, mean_peak_percent: number?, mid_window_resets, by_month: [{ month, windows, exhausted, cut_short, mean_peak_percent }] }?` over closed windows |
| `tools[].months[]` | Tokens plus `month: "YYYY-MM"`, `complete: boolean`, `days_covered: number`, `multiple: number?` (plan-covered cost over the plan price), `projected_cost_usd: number?` (partial months at their pace), `credit_funded_cost_usd: number?` |
| `tools[].complete_months` | integer |
| `tools[].mean_monthly_cost_usd` | number? |
| `tools[].mean_multiple` | number? |
| `tools[].pace` | `{ days: number, monthly_cost_usd: number?, multiple: number? }?` |
| `tools[].credits` | `{ first_at, last_at: string, first_balance, last_balance, spent, spent_at_limit, spent_near_resets, added: number, credit_funded: Tokens }?` (Codex) |

`Capacity` is `{ tokens, billable_tokens, requests, sessions, output_tokens,
estimated_cost_usd, reference_cost_usd }`, all numbers scaled to one whole
window (`billable_tokens` is uncached input plus output;
`reference_cost_usd` prices the same tokens at `reference_model` rates).
Months are local calendar months; `complete` is false for the current month
and the first month seen. The mean fields cover complete months, or every
month when none is complete. `mean_peak_percent` covers only windows that
ran to their reset time; `cut_short` counts those a mid-window reset ended.
Credit-funded requests (made while a window read 100 % and the balance fell)
are left out of `multiple` and `pace`.

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
children, including children in another tool, are already covered by their parent.
A cross-tool tree contributes time to its root session's project and tool. An unlinked child counts as a
top-level session. Token and session-count fields retain their existing meaning.

## `query agents`

| Field | Type |
|-------|------|
| `generated_at` | string |
| `since` | string? |
| `coverage` | object keyed by tool: `{supported: boolean, note: string}` |
| `totals` | `{top_level_sessions, sessions_with_subagents, subagent_sessions, unlinked_subagent_sessions, cross_tool_links, timing_matched_links, parents_with_imported_children}`, all integers |
| `subagent_token_share` | `{subagent_tokens: integer, total_tokens: integer, share: number?}` |
| `histogram[]` | `{bucket: string, min: integer, max: integer?, sessions: integer}` |
| `by_tool[]` | `{tool, top_level_sessions, sessions_with_subagents, subagent_sessions, unlinked_subagent_sessions, subagent_tokens, total_tokens: integer-valued, subagent_token_share: number?}` (`tool` is a string); also `cross_tool_links`, `timing_matched_links`, `parents_with_imported_children` (integers) |
| `model_mix` | `{main_thread: [ModelMixEntry], subagent: [ModelMixEntry]}`; `ModelMixEntry` is `{model: string?, requests: integer, tokens: integer, request_share: number}` |
| `subagent_efforts[]` | `{effort: string?, requests: integer}` |
| `heaviest_fanouts[]` | `{tool: string, session_id: string, title: string?, subagent_count: integer, subagent_models: [{model: string?, requests}], subagent_efforts: [{effort: string?, requests}], subagent_tokens: integer, total_tokens: integer, subagent_token_share: number?, max_concurrent_fanout: integer, display_title: string?, title_source: string?}`, at most 10 |

`cross_tool_links` counts imported links whose parent and child tools differ.
`timing_matched_links` counts imported links with `lineage_source: timing`,
including same-tool matches. These counts can overlap. `parents_with_imported_children`
counts distinct parents with a `t3` or `timing` child. Per-tool link counts
refer to the parent's tool. Tree metrics, including fan-outs and per-tree token
shares, include descendants across tools. Tool filters select the root's tool
for tree metrics and the request's tool for the overall token share and model
mix. With `--since`, a link counts when its child has a request in the window.
The dashboard coverage note uses these per-tool counts.

Internal `codex-auto-review` requests are excluded at ingest. Sources containing
only those requests create no sessions or event timelines. The v7 migration
removes their existing requests and sessions and rewinds affected sources to
correct mixed timelines on the next scan. They contribute no requests, tokens,
cost, session count, active time, or model entries to any report.

Codex sessions with `agent_type: guardian` are also excluded at ingest,
regardless of their model. They create no sessions, requests, or events and
contribute no subagent counts or fan-out. Source offsets and parser cursors
remain so incremental scans skip excluded data. Migration v8 removes stored
guardian sessions, requests, and events and refreshes cached ancestor active
time. Manual labels survive. This exclusion adds no JSON fields.

## Session display titles

Session reports, ambiguous session candidates, and `heaviest_fanouts[]` add
nullable `display_title` and `title_source`. Existing `title` stays the raw
first line of the opening prompt. The display title uses the harness headline,
then the T3 title, then the first line. Codex `name` or index `thread_name`
uses `codex-name`; the weaker SQLite `title` uses `codex-title`.
Antigravity summaries use `antigravity-summary`. Remaining sources are `t3`
and `first-line`. Display titles collapse whitespace and keep at most 120
characters. With no title, `display_title` is null and the source is
`first-line` after a scan. A title set with the `title` command uses
`manual` and wins over every other source. MCP returns the same additive
fields. Public snapshots contain none of these titles.

## `query untitled`

An array, most-highlighted first. Empty when every highlighted session has a
headline.

```json
[
  {
    "tool": "claude-code",
    "session_id": "...",
    "title_source": "first-line",
    "title": "Read the scientific handoff at ...",
    "path": "/home/demo/.claude/projects/-home-demo-proj/....jsonl",
    "transcript": "/home/demo/.claude/projects/-home-demo-proj/....jsonl",
    "pages": ["4w", "4w/claude-code", "12w"]
  }
]
```

`title_source` is `first-line` or `codex-title`; sessions with a harness,
T3, or hand-set headline are left out. `path` is the raw log the session
was read from (null when its source row is gone). `transcript` is the file to
summarize from: for Antigravity the harness's
`brain/<id>/.system_generated/logs/transcript.jsonl` when it exists, else
`path`. `pages` are the dashboard pages that highlight the session, as
`<window>` or `<window>/<tool>` for `1w`, `4w`, `12w`, `all`.
