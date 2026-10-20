# usage-habit-report

A local store of token usage from AI coding harnesses. `scan` reads the
harness logs into SQLite and `query` reports on them. Claude Code is supported
now; Codex and Antigravity are planned.

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
| Rates  | `~/.config/usage-habit-report/rates.json`     |

`XDG_DATA_HOME` and `XDG_CONFIG_HOME` override the parent directories.
`--db <path>` (or `USAGE_HABIT_REPORT_DB`) points every command at a
different store. `--offline` (or `USAGE_HABIT_REPORT_OFFLINE=1`) stops every
command from fetching model rates; see [Pricing](#pricing).

## Commands

### `scan`

```sh
usage-habit-report scan [--claude-dir ~/.claude/projects]
```

Reads every log file from the byte offset where the previous scan stopped,
up to the last complete line, in one transaction per file. A partial last
line is left for the next scan. A file smaller than its stored offset is
re-read from byte 0 after its old rows are dropped. Output:

```
claude-code: 120 files seen, 120 files read, 250000000 bytes read, 5000 rows added, 120 sessions added
elapsed: 0.25 s
priced: 5000 rows checked, 4990 with an estimated cost, 10 without a price
```

A second scan over unchanged logs reports `0 files read, 0 bytes read`.
After reading, `scan` runs the price step (see [Pricing](#pricing)).

### `query summary`

```sh
usage-habit-report query summary [--json] [--since 12h|7d|2w]
```

Token totals grouped by tool and model. `--since` keeps requests whose
timestamp falls within the last N hours (`h`), days (`d`), or weeks (`w`).
Without `--json` it prints a table whose `est. cost` column is the estimated
cost in USD; a `*` marks a figure that leaves out requests with no price.

JSON shape:

```json
{
  "generated_at": "2026-10-20T04:10:00Z",
  "since": "2026-10-13T04:10:00Z",
  "rates_fetched_at": "2026-10-20T04:19:17Z",
  "totals": {
    "requests": 5000,
    "input_tokens": 25000,
    "cache_read_input_tokens": 480000000,
    "cache_creation_input_tokens": 19000000,
    "output_tokens": 4900000,
    "estimated_cost_usd": 412.37,
    "unpriced_requests": 0
  },
  "groups": [
    {
      "tool": "claude-code",
      "model": "claude-example-model",
      "requests": 3600,
      "input_tokens": 7500,
      "cache_read_input_tokens": 370000000,
      "cache_creation_input_tokens": 12000000,
      "output_tokens": 3000000,
      "estimated_cost_usd": 301.55,
      "unpriced_requests": 0
    }
  ]
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `generated_at` | string | RFC 3339 UTC time the summary was computed |
| `since` | string or null | RFC 3339 UTC lower bound from `--since`; null means all time |
| `rates_fetched_at` | string | RFC 3339 UTC time the prices in use were fetched from LiteLLM |
| `totals` | object | Sum over all groups |
| `groups[]` | array | One entry per (tool, model), ordered by tool, then total tokens descending |
| `tool` | string | `claude-code` (later also `codex`, `antigravity`) |
| `model` | string or null | Raw model name from the log; null when the log has none |
| `requests` | integer | Billed model calls |
| `input_tokens` | integer | Uncached input tokens |
| `cache_read_input_tokens` | integer | Input tokens read from the prompt cache |
| `cache_creation_input_tokens` | integer | Input tokens written to the prompt cache |
| `output_tokens` | integer | Output tokens (includes thinking) |
| `estimated_cost_usd` | number or null | Estimated USD at list prices, summed over requests that have a price; null when none do |
| `unpriced_requests` | integer | Requests left out of `estimated_cost_usd` because their model has no price |

Subagent requests are included in the totals of the model that served them.

### `query models`

```sh
usage-habit-report query models [--json]
```

One line per (tool, raw model name): the canonical name, the LiteLLM key it
priced against, `priced` yes/no, and the request count. Unpriced names sort
first, so a name the canonical table does not cover shows up at the top.
JSON is an array of `{tool, model, canonical_model, litellm_key, priced,
requests}`.

### `rates update` and `rates apply`

```sh
usage-habit-report rates update        # fetch LiteLLM prices, re-price every request
usage-habit-report rates apply [--all] # price requests without a cost (every request with --all)
```

`rates update` fails when the fetch fails; it never falls back.

## Pricing

Every request row gets `canonical_model` and `cost_usd`. All dollar figures
are estimates at public list prices: no batch, priority, flex, regional, or
subscription pricing.

**Rates.** Prices come from LiteLLM's
[`model_prices_and_context_window.json`](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json).
`rates.json` keeps the entries whose `litellm_provider` is `anthropic`,
`openai`, `gemini` (Google AI Studio, `gemini/…` keys), or
`vertex_ai-language-models` (bare `gemini-…` keys) and whose `mode` is
`chat`, `responses`, or `completion`. Re-hosted copies (Bedrock, Azure,
Vertex AI Anthropic, OpenRouter, …) are left out. Each entry keeps
`litellm_provider`, `mode`, `input_cost_per_token`, `output_cost_per_token`,
`cache_read_input_token_cost`, `cache_creation_input_token_cost`,
`cache_creation_input_token_cost_above_1hr`, and the tiered
`<field>_above_<N>k_tokens` variants. The file is
`{"fetched_at", "source", "models": {<litellm key>: {...}}}`.

**Refresh.** A command that needs prices (`scan`, `query summary`,
`query models`, `rates apply`) first checks `rates.json`. When the file is
missing, unreadable, or its `fetched_at` is more than 7 days old, it prints
`refreshing model rates from LiteLLM…` on stderr and fetches (10 s timeout).
`--offline` skips the fetch. When the fetch fails, the command notes it on
stderr and uses the old `rates.json`, or, without one, the snapshot compiled
into the binary (`src/rates_snapshot.json`, a `rates update` output
committed to the repository). Refresh never happens in the background.

**Canonical names** (`src/models.rs`). Raw names are lowercased; provider
prefixes (`models/`, `anthropic/`, `gemini/`, …) and Vertex `@` pins are
dropped; enum-style names are rewritten (`claude_v5_opus_thinking` ->
`claude-opus-5`, `MODEL_CLAUDE_4_5_SONNET_THINKING` -> `claude-sonnet-4-5`);
snapshot dates (`-20251001`, `-2025-08-07`) and effort or mode suffixes
(`-high`, `-medium`, `-low`, `-thinking`, `-tiered`, …) are stripped. `max`
is stripped only from Claude names, since `gpt-5.1-codex-max` is its own
model. Opaque placeholders (`MODEL_PLACEHOLDER_M26`) get no canonical name.
The LiteLLM key is the canonical name, except Gemini, which tries
`gemini/<name>`, `<name>`, then the `-preview` forms of both.

**Cost per request.**

```
cost = input × input_rate + cache_read × cache_read_rate
     + cache_creation × cache_creation_rate + output × output_rate
```

- `input` is uncached input only. Each harness ingester stores it that way.
- Output already includes reasoning tokens (Anthropic and OpenAI count them
  in output; ingesters store `output_tokens` the same way for every tool).
  `reasoning_tokens` is never added again.
- Tiered rates (`_above_200k_tokens`, `_above_272k_tokens`, …) apply to every
  token of a request whose prompt (uncached + cache-read + cache-write input)
  exceeds the threshold. Rates a tier does not list stay at the base rate.
- Cache writes use the 5-minute rate. The 1-hour rate is never used: no
  harness records the TTL of a cache write.
- A missing cache-read or cache-write rate falls back to the input rate.
- A model with no LiteLLM entry, or no input or output rate, gets
  `cost_usd = NULL`. No default rate is ever used.

**Price step.** After `scan`, and in every command that needs prices, rows
with a null `canonical_model` or `cost_usd` are priced. After `rates update`,
or when the command just refreshed the rates, every row is re-priced. A
trigger clears `canonical_model` and `cost_usd` whenever a later merge
changes a row's model or token counts, so a row priced mid-stream is priced
again with its final counts.

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

## Store schema (v1)

The schema version is kept in `PRAGMA user_version`; migrations live in
`src/store.rs`.

- `sources`: one row per log file, with `path`, `offset` (bytes consumed),
  `size`, `mtime_ms`, and `scanned_at`.
- `sessions`: one row per `(tool, session_id)`, with `is_subagent`,
  `parent_session_id`, `agent_type`, `title`, `cwd`, `started_at_ms`,
  `ended_at_ms`, and `user_turns`. The nullable columns `model_source`,
  `project`, `t3_thread_id`, `t3_title`, `label`, and `label_source` are
  reserved for later tickets.
- `requests`: one row per `(tool, request_key)`, with `session_id`,
  `is_subagent`, `source_id`, `ts_ms`, `model`, `effort`, and the four token
  counts. `canonical_model` and `cost_usd` are filled by the price step and
  stay null for unpriced models. The nullable column `reasoning_tokens` is
  reserved for later tickets.
- Trigger `requests_unprice` (ticket 04): clears `canonical_model` and
  `cost_usd` when a row's model or token counts change.

## Tests

```sh
cargo test
```

The fixtures in `tests/fixtures/` are hand-written. Never commit real
transcript content: this repository is public.
