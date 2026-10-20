//! Codex rollout parsing and incremental scan.
//!
//! Layout under the Codex home (`$CODEX_HOME`, default `~/.codex`):
//! - `sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl` are live rollouts.
//! - `archived_sessions/rollout-<time>-<id>.jsonl` are archived ones.
//! - Either may be zstd-compressed as `.jsonl.zst`. Codex compresses older
//!   rollouts in place, so a file can change from `.jsonl` to `.jsonl.zst`,
//!   or move from `sessions/` to `archived_sessions/`, between scans. A file
//!   is identified by its stem (name without `.jsonl[.zst]`); a new path whose
//!   stem matches a tracked source that no longer exists takes over that
//!   source and its offset.
//! - A long session can span several files: `rollout-<time>-<id>_<window>.jsonl`
//!   continues session `<id>`. The session id always comes from the first
//!   `session_meta` record (`payload.id`), not the file name.
//!
//! Each line is `{"timestamp", "ordinal", "type", "payload"}`. The parser uses:
//! - `session_meta`: id, cwd, subagent parent (`parent_thread_id`), subagent
//!   kind (`source.subagent`), and fork details. Only the first one in a file
//!   counts; forks copy their parent's `session_meta` in after their own.
//! - `turn_context`: `model` and `effort` for the requests that follow.
//! - `event_msg` / `token_count`: `info.total_token_usage` is cumulative,
//!   `info.last_token_usage` is the latest model call. A request is recorded
//!   each time the cumulative total changes; repeats with an unchanged total
//!   are skipped. Its counts are `last_token_usage` (they equal the cumulative
//!   delta except where the counter restarts; see README).
//! - `response_item` / `message` with `role: "user"`: a user turn, unless every
//!   text block is harness-injected context.
//!
//! Forked rollouts (`forked_from_id` set) start with a copy of the parent's
//! history. Records before `subagent_history_start_ordinal` (or, when that is
//! absent, records written in the same second as the fork) are that copy:
//! they set the cumulative baseline and the model but add no requests or turns.

use crate::claude::{make_title, parse_ts_ms};
use crate::scan::{ScanReport, ScanStats, now_ms};
use crate::store::{self, RequestRow, SessionUpdate, Store};
use anyhow::{Context, Result};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use std::borrow::Cow;
use std::collections::HashMap;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::{Instant, UNIX_EPOCH};
use walkdir::WalkDir;

pub const TOOL: &str = "codex";

/// Text blocks that start with one of these tags are written by the harness,
/// not typed by a person. A user message made only of such blocks is not a
/// turn and never becomes the title.
const INJECTED_TAGS: &[&str] = &[
    "environment_context",
    "user_instructions",
    "recommended_plugins",
    "skill",
    "external_codex_apps_open_page",
    "turn_aborted",
    "send_user_message_question_reply",
    "codex_delegation",
    "realtime_delegation",
    "heartbeat",
    "in-app-browser-context",
    "subagent_notification",
    "codex_internal_context",
    "guardian_tool_descriptions",
    "user_shell_command",
    "image",
];

/// Markdown headers the harness injects as whole blocks.
const INJECTED_HEADERS: &[&str] = &["# AGENTS.md instructions"];

/// Codex Desktop wraps a prompt in IDE context (`# Files mentioned by the
/// user`, `# Files pasted by ...`, `# Selected text`, `# Context from my IDE
/// setup`); the prompt follows a `## My request...` line.
const IDE_WRAPPERS: &[&str] = &["# Context from", "# Files ", "# Selected text"];
const IDE_REQUEST_HEADER: &str = "## My request";

/// Forks without `subagent_history_start_ordinal`: records this close to the
/// fork's first record are the copied parent history.
const FORK_COPY_WINDOW_MS: i64 = 1000;

/// `$CODEX_HOME`, else `~/.codex`.
pub fn default_home() -> Result<PathBuf> {
    match std::env::var_os("CODEX_HOME") {
        Some(v) if !v.is_empty() => Ok(PathBuf::from(v)),
        _ => Ok(std::env::var_os("HOME")
            .map(PathBuf::from)
            .context("HOME is not set")?
            .join(".codex")),
    }
}

/// The two folders that hold rollouts.
pub fn rollout_dirs(home: &Path) -> [PathBuf; 2] {
    [home.join("sessions"), home.join("archived_sessions")]
}

/// `rollout-...` file name without `.jsonl` / `.jsonl.zst`, if this is a rollout.
pub fn rollout_stem(path: &Path) -> Option<&str> {
    let name = path.file_name()?.to_str()?;
    if !name.starts_with("rollout-") {
        return None;
    }
    name.strip_suffix(".jsonl.zst")
        .or_else(|| name.strip_suffix(".jsonl"))
}

fn is_compressed(path: &Path) -> bool {
    path.extension().is_some_and(|e| e == "zst")
}

/// Session id from the file name, used only if a file has no `session_meta`:
/// the first UUID after the timestamp (`<id>` in `<id>_<window>`).
fn id_from_stem(stem: &str) -> Option<String> {
    // rollout-YYYY-MM-DDTHH-MM-SS-<uuid>[_<uuid>]
    let rest = stem.get("rollout-YYYY-MM-DDTHH-MM-SS-".len()..)?;
    let id = rest.split('_').next()?;
    (id.len() == 36).then(|| id.to_string())
}

/// Token counts as Codex reports them. `input_tokens` includes cached input.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Usage {
    pub input_tokens: i64,
    pub cached_input_tokens: i64,
    pub cache_write_input_tokens: i64,
    pub output_tokens: i64,
    pub reasoning_output_tokens: i64,
}

impl Usage {
    fn minus(&self, o: &Usage) -> Usage {
        Usage {
            input_tokens: self.input_tokens - o.input_tokens,
            cached_input_tokens: self.cached_input_tokens - o.cached_input_tokens,
            cache_write_input_tokens: self.cache_write_input_tokens - o.cache_write_input_tokens,
            output_tokens: self.output_tokens - o.output_tokens,
            reasoning_output_tokens: self.reasoning_output_tokens - o.reasoning_output_tokens,
        }
    }

    fn any_negative(&self) -> bool {
        self.input_tokens < 0
            || self.cached_input_tokens < 0
            || self.cache_write_input_tokens < 0
            || self.output_tokens < 0
            || self.reasoning_output_tokens < 0
    }
}

/// Parser state that must survive between incremental scans of one file.
/// Stored as JSON in `codex_cursors.state`.
#[derive(Debug, Default, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Cursor {
    pub session_id: Option<String>,
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
    pub agent_type: Option<String>,
    pub meta_seen: bool,
    /// Forks: records with a lower ordinal are copied parent history.
    pub cut_ordinal: Option<i64>,
    /// Forks without a cut ordinal: records at or before this time are copies.
    pub cut_until_ms: Option<i64>,
    /// Last cumulative `total_token_usage` seen.
    pub prev_total: Option<Usage>,
    pub model: Option<String>,
    pub effort: Option<String>,
    /// Lines consumed so far; stands in for `ordinal` when a record lacks it.
    pub next_line: i64,
    /// A `token_usage_record` has been seen: requests come from those records
    /// from here on, and `token_count` only tracks the cumulative baseline.
    pub per_response: bool,
}

/// Data-quality counts, reported by `scan`.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Anomalies {
    /// `cached_input_tokens` exceeded `input_tokens`; uncached input clamped to 0.
    pub cached_exceeds_input: u64,
    /// `last_token_usage` disagreed with the change in the cumulative total.
    pub usage_mismatch: u64,
    /// Of those, the cumulative total went down (counter restarted).
    pub counter_resets: u64,
    /// `token_count` events whose cumulative total had not changed (skipped).
    pub repeated_token_counts: u64,
    /// `token_count` events inside a fork's copy of its parent's history (skipped).
    pub copied_history_skipped: u64,
    /// `token_count` events with no usage (rate-limit only; skipped).
    pub token_count_without_info: u64,
    /// `token_usage_record`s for another thread than the file's session (skipped).
    pub foreign_usage_records: u64,
}

impl Anomalies {
    fn add(&mut self, o: &Anomalies) {
        self.cached_exceeds_input += o.cached_exceeds_input;
        self.usage_mismatch += o.usage_mismatch;
        self.counter_resets += o.counter_resets;
        self.repeated_token_counts += o.repeated_token_counts;
        self.copied_history_skipped += o.copied_history_skipped;
        self.token_count_without_info += o.token_count_without_info;
        self.foreign_usage_records += o.foreign_usage_records;
    }
}

/// One request: the shared row plus the reasoning count Codex reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexRequest {
    pub row: RequestRow,
    /// Part of `output_tokens`, not in addition to it.
    pub reasoning_tokens: i64,
}

#[derive(Deserialize)]
struct Rec<'a> {
    #[serde(borrow)]
    timestamp: Option<Cow<'a, str>>,
    ordinal: Option<i64>,
    #[serde(rename = "type", borrow)]
    kind: Cow<'a, str>,
    #[serde(borrow)]
    payload: Option<&'a RawValue>,
}

#[derive(Deserialize)]
struct Meta {
    id: Option<String>,
    session_id: Option<String>,
    cwd: Option<String>,
    parent_thread_id: Option<String>,
    forked_from_id: Option<String>,
    subagent_history_start_ordinal: Option<i64>,
    thread_source: Option<String>,
    source: Option<serde_json::Value>,
}

#[derive(Deserialize)]
struct TurnContext {
    cwd: Option<String>,
    model: Option<String>,
    effort: Option<String>,
    collaboration_mode: Option<CollaborationMode>,
}

#[derive(Deserialize)]
struct CollaborationMode {
    settings: Option<CollaborationSettings>,
}

#[derive(Deserialize)]
struct CollaborationSettings {
    reasoning_effort: Option<String>,
}

#[derive(Deserialize)]
struct EventPayload {
    #[serde(rename = "type")]
    kind: Option<String>,
    info: Option<TokenInfo>,
}

#[derive(Deserialize)]
struct TokenInfo {
    total_token_usage: Option<Usage>,
    last_token_usage: Option<Usage>,
}

#[derive(Deserialize)]
struct UsageRecord {
    thread_id: Option<String>,
    response_id: Option<String>,
    usage: Option<Usage>,
}

#[derive(Deserialize)]
struct ItemPayload {
    #[serde(rename = "type")]
    kind: Option<String>,
    role: Option<String>,
    content: Option<Vec<Block>>,
}

#[derive(Deserialize)]
struct Block {
    text: Option<String>,
}

/// Everything learned from a run of lines of one rollout.
#[derive(Debug)]
pub struct Chunk {
    pub cursor: Cursor,
    stem: String,
    pub requests: Vec<CodexRequest>,
    pub title: Option<String>,
    pub cwd: Option<String>,
    pub started_at_ms: Option<i64>,
    pub ended_at_ms: Option<i64>,
    pub user_turns: i64,
    pub bad_lines: u64,
    pub anomalies: Anomalies,
}

impl Chunk {
    /// Continue parsing the file `stem` from a saved cursor.
    pub fn new(stem: &str, cursor: Cursor) -> Self {
        Chunk {
            cursor,
            stem: stem.to_string(),
            requests: Vec::new(),
            title: None,
            cwd: None,
            started_at_ms: None,
            ended_at_ms: None,
            user_turns: 0,
            bad_lines: 0,
            anomalies: Anomalies::default(),
        }
    }

    /// Session id for this file: from `session_meta`, else the file name.
    pub fn session_id(&self) -> Option<String> {
        self.cursor
            .session_id
            .clone()
            .or_else(|| id_from_stem(&self.stem))
    }

    /// Feed one complete line (without the trailing newline).
    pub fn push_line(&mut self, line: &[u8]) {
        let line = line.trim_ascii();
        if line.is_empty() {
            return;
        }
        let line_no = self.cursor.next_line;
        self.cursor.next_line += 1;

        // Most lines (reasoning, tool calls, item events) are irrelevant and
        // large; decide from the leading fields whether to parse at all.
        let ts_ms = sniff_str(line, b"{\"timestamp\":\"").and_then(parse_ts_ms);
        if let Some(t) = ts_ms {
            self.started_at_ms = Some(self.started_at_ms.map_or(t, |s| s.min(t)));
            self.ended_at_ms = Some(self.ended_at_ms.map_or(t, |e| e.max(t)));
        }
        if !wanted(line) {
            return;
        }

        let rec: Rec = match serde_json::from_slice(line) {
            Ok(r) => r,
            Err(_) => {
                self.bad_lines += 1;
                return;
            }
        };
        let ts_ms = ts_ms.or_else(|| rec.timestamp.as_deref().and_then(parse_ts_ms));
        let ordinal = rec.ordinal.unwrap_or(line_no);
        let Some(payload) = rec.payload else { return };
        let payload = payload.get();
        match rec.kind.as_ref() {
            "session_meta" => {
                if !self.cursor.meta_seen {
                    match serde_json::from_str::<Meta>(payload) {
                        Ok(m) => self.apply_meta(m, ts_ms),
                        Err(_) => self.bad_lines += 1,
                    }
                }
            }
            "turn_context" => match serde_json::from_str::<TurnContext>(payload) {
                Ok(tc) => self.apply_turn_context(tc, self.is_copied(ordinal, ts_ms)),
                Err(_) => self.bad_lines += 1,
            },
            "event_msg" => match serde_json::from_str::<EventPayload>(payload) {
                Ok(ev) if ev.kind.as_deref() == Some("token_count") => {
                    self.push_token_count(ev.info, ordinal, ts_ms)
                }
                Ok(_) => {}
                Err(_) => self.bad_lines += 1,
            },
            "token_usage_record" => match serde_json::from_str::<UsageRecord>(payload) {
                Ok(u) => self.push_usage_record(u, ordinal, ts_ms),
                Err(_) => self.bad_lines += 1,
            },
            "response_item" => match serde_json::from_str::<ItemPayload>(payload) {
                Ok(it)
                    if it.kind.as_deref() == Some("message")
                        && it.role.as_deref() == Some("user") =>
                {
                    if !self.is_copied(ordinal, ts_ms) {
                        self.push_user(it.content.unwrap_or_default());
                    }
                }
                Ok(_) => {}
                Err(_) => self.bad_lines += 1,
            },
            _ => {}
        }
    }

    fn is_copied(&self, ordinal: i64, ts_ms: Option<i64>) -> bool {
        self.cursor.cut_ordinal.is_some_and(|c| ordinal < c)
            || matches!((self.cursor.cut_until_ms, ts_ms), (Some(c), Some(t)) if t <= c)
    }

    fn apply_meta(&mut self, m: Meta, ts_ms: Option<i64>) {
        let c = &mut self.cursor;
        c.meta_seen = true;
        c.session_id = m.id.or(m.session_id).filter(|s| !s.is_empty());
        let (sub_kind, sub_parent) = subagent_source(m.source.as_ref());
        c.parent_session_id = m.parent_thread_id.or(sub_parent);
        c.is_subagent = c.parent_session_id.is_some()
            || sub_kind.is_some()
            || m.thread_source.as_deref() == Some("subagent");
        c.agent_type = sub_kind;
        if m.forked_from_id.is_some() {
            match m.subagent_history_start_ordinal {
                Some(o) => c.cut_ordinal = Some(o),
                None => c.cut_until_ms = ts_ms.map(|t| t + FORK_COPY_WINDOW_MS),
            }
        }
        if self.cwd.is_none() {
            self.cwd = m.cwd.filter(|s| !s.is_empty());
        }
    }

    fn apply_turn_context(&mut self, tc: TurnContext, copied: bool) {
        if let Some(m) = tc.model.filter(|m| !m.is_empty()) {
            self.cursor.model = Some(m);
        }
        let effort = tc.effort.or_else(|| {
            tc.collaboration_mode
                .and_then(|c| c.settings)
                .and_then(|s| s.reasoning_effort)
        });
        if let Some(e) = effort.filter(|e| !e.is_empty()) {
            self.cursor.effort = Some(e);
        }
        if self.cwd.is_none() && !copied {
            self.cwd = tc.cwd.filter(|s| !s.is_empty());
        }
    }

    fn push_token_count(&mut self, info: Option<TokenInfo>, ordinal: i64, ts_ms: Option<i64>) {
        let Some((total, last)) =
            info.and_then(|i| Some((i.total_token_usage?, i.last_token_usage)))
        else {
            self.anomalies.token_count_without_info += 1;
            return;
        };
        let prev = self.cursor.prev_total.replace(total);
        if self.cursor.per_response {
            return;
        }
        if prev == Some(total) {
            self.anomalies.repeated_token_counts += 1;
            return;
        }
        if self.is_copied(ordinal, ts_ms) {
            self.anomalies.copied_history_skipped += 1;
            return;
        }
        let delta = prev.map(|p| total.minus(&p));
        if delta.is_some_and(|d| d.any_negative()) {
            self.anomalies.counter_resets += 1;
        }
        let u = match (last, delta) {
            (Some(last), Some(d)) => {
                if d != last {
                    self.anomalies.usage_mismatch += 1;
                }
                last
            }
            (Some(last), None) => last,
            // No per-call figure: the delta, or the whole total after a restart.
            (None, Some(d)) if !d.any_negative() => d,
            (None, _) => total,
        };
        self.push_request(format!("{}#{}", self.stem, ordinal), u, ts_ms);
    }

    /// Newer Codex writes one `token_usage_record` per model response, just
    /// before the matching `token_count`. It is the more complete source: some
    /// responses get a `token_count` whose total never moved (`last` all zero).
    fn push_usage_record(&mut self, rec: UsageRecord, ordinal: i64, ts_ms: Option<i64>) {
        if let (Some(t), Some(s)) = (&rec.thread_id, &self.cursor.session_id)
            && t != s
        {
            self.anomalies.foreign_usage_records += 1;
            return;
        }
        self.cursor.per_response = true;
        if self.is_copied(ordinal, ts_ms) {
            self.anomalies.copied_history_skipped += 1;
            return;
        }
        let Some(u) = rec.usage else {
            self.bad_lines += 1;
            return;
        };
        let key = rec
            .response_id
            .filter(|r| !r.is_empty())
            .unwrap_or_else(|| format!("{}#{}", self.stem, ordinal));
        self.push_request(key, u, ts_ms);
    }

    fn push_request(&mut self, request_key: String, u: Usage, ts_ms: Option<i64>) {
        let Some(ts) = ts_ms else {
            self.bad_lines += 1;
            return;
        };
        let cached = u.cached_input_tokens.max(0);
        let cache_write = u.cache_write_input_tokens.max(0);
        let mut uncached = u.input_tokens - cached - cache_write;
        if uncached < 0 {
            self.anomalies.cached_exceeds_input += 1;
            uncached = 0;
        }
        self.requests.push(CodexRequest {
            row: RequestRow {
                request_key,
                ts_ms: ts,
                model: self.cursor.model.clone(),
                effort: self.cursor.effort.clone(),
                input_tokens: uncached,
                cache_read_input_tokens: cached,
                cache_creation_input_tokens: cache_write,
                output_tokens: u.output_tokens.max(0),
            },
            reasoning_tokens: u.reasoning_output_tokens.max(0),
        });
    }

    fn push_user(&mut self, blocks: Vec<Block>) {
        let typed: Vec<&str> = blocks
            .iter()
            .filter_map(|b| b.text.as_deref())
            .filter(|t| !is_injected(t))
            .collect();
        if typed.is_empty() {
            return;
        }
        self.user_turns += 1;
        if self.title.is_none() {
            self.title = typed.iter().find_map(|t| title_from(t));
        }
    }
}

/// `(agent_type, parent id)` from `session_meta.source.subagent`:
/// `{"other": "guardian"}` gives `guardian`; `{"thread_spawn": {...}}` gives
/// its `agent_role`, or `thread_spawn` when the role is null.
fn subagent_source(source: Option<&serde_json::Value>) -> (Option<String>, Option<String>) {
    let Some(sub) = source.and_then(|s| s.get("subagent")) else {
        return (None, None);
    };
    match sub {
        serde_json::Value::String(s) => (Some(s.clone()), None),
        serde_json::Value::Object(m) => {
            let Some((k, v)) = m.iter().next() else {
                return (Some("subagent".into()), None);
            };
            let kind = match v {
                serde_json::Value::String(s) => s.clone(),
                serde_json::Value::Object(o) => o
                    .get("agent_role")
                    .and_then(|r| r.as_str())
                    .filter(|r| !r.is_empty())
                    .unwrap_or(k)
                    .to_string(),
                _ => k.clone(),
            };
            let parent = v
                .get("parent_thread_id")
                .and_then(|p| p.as_str())
                .map(str::to_string);
            (Some(kind), parent)
        }
        _ => (Some("subagent".into()), None),
    }
}

/// True for harness-written context blocks (see `INJECTED_TAGS`).
pub fn is_injected(text: &str) -> bool {
    let s = text.trim_start();
    if INJECTED_HEADERS.iter().any(|h| s.starts_with(h)) {
        return true;
    }
    let Some(rest) = s.strip_prefix('<') else {
        return false;
    };
    let rest = rest.strip_prefix('/').unwrap_or(rest);
    let end = rest
        .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-'))
        .unwrap_or(rest.len());
    INJECTED_TAGS.contains(&&rest[..end])
}

/// Title candidate from one typed block. IDE-wrapped prompts give the text
/// after their `## My request` line, or nothing if that line is missing.
pub fn title_from(text: &str) -> Option<String> {
    let s = text.trim_start();
    let mut lines = s.lines();
    if lines
        .by_ref()
        .any(|l| l.trim_start().starts_with(IDE_REQUEST_HEADER))
    {
        let rest: Vec<&str> = lines.collect();
        return make_title(&rest.join("\n"));
    }
    if IDE_WRAPPERS.iter().any(|w| s.starts_with(w)) {
        return None;
    }
    make_title(s)
}

/// Value of a string field that starts exactly at `prefix` at byte 0.
fn sniff_str<'a>(line: &'a [u8], prefix: &[u8]) -> Option<&'a str> {
    let rest = line.strip_prefix(prefix)?;
    let end = rest.iter().position(|&b| b == b'"')?;
    std::str::from_utf8(&rest[..end]).ok()
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// Whether a line can matter, judged from its leading `type` fields. Lines
/// whose layout is unexpected are parsed (the answer is "maybe").
fn wanted(line: &[u8]) -> bool {
    const TYPE: &[u8] = b"\"type\":\"";
    // Top-level keys come first: timestamp, ordinal, type.
    let head = &line[..line.len().min(160)];
    let Some(i) = find(head, TYPE) else {
        return true;
    };
    // Only trust a `type` key that sits at the top level.
    if head[1..i].contains(&b'{') {
        return true;
    }
    let rest = &line[i + TYPE.len()..];
    let Some(end) = rest.iter().position(|&b| b == b'"') else {
        return true;
    };
    let top = &rest[..end];
    let sub_wanted: &[u8] = match top {
        b"session_meta" | b"turn_context" | b"token_usage_record" => return true,
        b"event_msg" => b"token_count",
        b"response_item" => b"message",
        _ => return false,
    };
    const PAYLOAD: &[u8] = b",\"payload\":{\"type\":\"";
    let after = &rest[end + 1..];
    match after.strip_prefix(PAYLOAD) {
        Some(p) => p.starts_with(sub_wanted) && p.get(sub_wanted.len()) == Some(&b'"'),
        None => true,
    }
}

/// Totals for one `scan_codex` run.
#[derive(Debug, Clone)]
pub struct CodexScanReport {
    pub report: ScanReport,
    pub anomalies: Anomalies,
    /// Files that took over a tracked source after being moved or compressed.
    pub files_relocated: u64,
    /// Second copies of a rollout already present under another path (skipped).
    pub duplicates_skipped: u64,
    /// Files that could not be read this time (for example a `.zst` still being written).
    pub files_unreadable: u64,
}

/// Scan `sessions/` and `archived_sessions/` under the Codex home.
/// Missing folders are not an error.
pub fn scan_codex(store: &mut Store, home: &Path) -> Result<CodexScanReport> {
    let start = Instant::now();
    let mut out = CodexScanReport {
        report: ScanReport {
            stats: ScanStats::default(),
            elapsed: Default::default(),
        },
        anomalies: Anomalies::default(),
        files_relocated: 0,
        duplicates_skipped: 0,
        files_unreadable: 0,
    };

    // One path per stem: prefer the path already tracked, then the plain file.
    let mut by_stem: HashMap<String, Vec<PathBuf>> = HashMap::new();
    for dir in rollout_dirs(home) {
        if !dir.is_dir() {
            continue;
        }
        for e in WalkDir::new(&dir)
            .follow_links(false)
            .into_iter()
            .filter_map(|e| e.ok())
        {
            if !e.file_type().is_file() {
                continue;
            }
            if let Some(stem) = rollout_stem(e.path()) {
                by_stem
                    .entry(stem.to_string())
                    .or_default()
                    .push(e.into_path());
            }
        }
    }
    let mut files: Vec<(String, PathBuf)> = Vec::with_capacity(by_stem.len());
    for (stem, mut paths) in by_stem {
        if paths.len() > 1 {
            let tracked: Vec<bool> = paths
                .iter()
                .map(|p| {
                    store::source_state(&store.conn, &p.to_string_lossy()).map(|s| s.is_some())
                })
                .collect::<Result<_>>()?;
            let mut order: Vec<usize> = (0..paths.len()).collect();
            order.sort_by_key(|&i| (!tracked[i], is_compressed(&paths[i]), paths[i].clone()));
            let keep = order[0];
            out.duplicates_skipped += paths.len() as u64 - 1;
            let p = paths.swap_remove(keep);
            files.push((stem, p));
        } else {
            files.push((stem, paths.pop().expect("non-empty")));
        }
    }
    // Rollout names start with their creation time, so parents precede subagents.
    files.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)));

    for (stem, path) in files {
        out.report.stats.files_seen += 1;
        scan_file(store, &stem, &path, &mut out)
            .with_context(|| format!("scanning {}", path.display()))?;
    }
    out.report.elapsed = start.elapsed();
    Ok(out)
}

struct Prior {
    id: i64,
    offset: u64,
    size: u64,
    mtime_ms: Option<i64>,
}

fn prior_source(conn: &rusqlite::Connection, path: &str) -> Result<Option<Prior>> {
    Ok(conn
        .query_row(
            "SELECT id, offset, size, mtime_ms FROM sources WHERE path = ?1",
            params![path],
            |r| {
                Ok(Prior {
                    id: r.get(0)?,
                    offset: r.get::<_, i64>(1)? as u64,
                    size: r.get::<_, i64>(2)? as u64,
                    mtime_ms: r.get(3)?,
                })
            },
        )
        .optional()?)
}

/// A tracked Codex source with the same stem whose file is gone: the rollout
/// was compressed or archived. Repoint it at `path` and keep its offset.
fn relocate(store: &mut Store, stem: &str, path: &str) -> Result<bool> {
    let candidates: Vec<(i64, String)> = {
        let mut stmt = store.conn.prepare(
            "SELECT id, path FROM sources WHERE tool = ?1 AND (path LIKE ?2 OR path LIKE ?3)",
        )?;
        stmt.query_map(
            params![
                TOOL,
                format!("%/{stem}.jsonl"),
                format!("%/{stem}.jsonl.zst")
            ],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?
        .collect::<rusqlite::Result<_>>()?
    };
    for (id, old) in candidates {
        if !Path::new(&old).exists() {
            store.conn.execute(
                "UPDATE sources SET path = ?2 WHERE id = ?1",
                params![id, path],
            )?;
            return Ok(true);
        }
    }
    Ok(false)
}

fn load_cursor(conn: &rusqlite::Connection, source_id: i64) -> Result<Cursor> {
    let state: Option<String> = conn
        .query_row(
            "SELECT state FROM codex_cursors WHERE source_id = ?1",
            params![source_id],
            |r| r.get(0),
        )
        .optional()?;
    Ok(state
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default())
}

/// Lines read from one file: the parse result and the bytes consumed
/// (decompressed bytes for `.zst`).
struct ReadResult {
    chunk: Chunk,
    consumed: u64,
}

/// Open `path` positioned at `offset` (decompressed offset for `.zst`).
/// `Ok(None)` means the content is now shorter than `offset`.
fn open_at(path: &Path, offset: u64) -> io::Result<Option<Box<dyn BufRead>>> {
    let mut file = File::open(path)?;
    if is_compressed(path) {
        let mut dec = BufReader::with_capacity(1 << 20, zstd::stream::read::Decoder::new(file)?);
        let skipped = io::copy(&mut (&mut dec).take(offset), &mut io::sink())?;
        if skipped < offset {
            return Ok(None);
        }
        Ok(Some(Box::new(dec)))
    } else {
        if file.metadata()?.len() < offset {
            return Ok(None);
        }
        file.seek(SeekFrom::Start(offset))?;
        Ok(Some(Box::new(BufReader::with_capacity(1 << 20, file))))
    }
}

fn read_lines(
    mut reader: Box<dyn BufRead>,
    compressed: bool,
    mut chunk: Chunk,
) -> io::Result<ReadResult> {
    let mut line = Vec::with_capacity(64 * 1024);
    let mut consumed = 0u64;
    loop {
        line.clear();
        let n = reader.read_until(b'\n', &mut line)?;
        if n == 0 {
            break;
        }
        let complete = line.last() == Some(&b'\n');
        // A plain file may still be growing: wait for the newline. A `.zst`
        // decoded to its end without error is finished, so its last line counts.
        if !complete && !compressed {
            break;
        }
        consumed += n as u64;
        chunk.push_line(if complete { &line[..n - 1] } else { &line[..] });
    }
    Ok(ReadResult { chunk, consumed })
}

fn scan_file(store: &mut Store, stem: &str, path: &Path, out: &mut CodexScanReport) -> Result<()> {
    let path_str = path.to_string_lossy().into_owned();
    let meta = std::fs::metadata(path)?;
    let size = meta.len();
    let mtime_ms = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as i64);
    let compressed = is_compressed(path);

    let mut prior = prior_source(&store.conn, &path_str)?;
    if prior.is_none() && relocate(store, stem, &path_str)? {
        out.files_relocated += 1;
        prior = prior_source(&store.conn, &path_str)?;
    }
    if let Some(p) = &prior {
        let unchanged = if compressed {
            p.size == size && p.mtime_ms == mtime_ms
        } else {
            p.offset == size
        };
        if unchanged {
            return Ok(());
        }
    }

    let mut offset = prior.as_ref().map_or(0, |p| p.offset);
    let mut cursor = match &prior {
        Some(p) => load_cursor(&store.conn, p.id)?,
        None => Cursor::default(),
    };
    let mut shrank = false;
    let opened = match open_at(path, offset) {
        Ok(Some(r)) => Ok(r),
        Ok(None) => {
            shrank = true;
            offset = 0;
            cursor = Cursor::default();
            open_at(path, 0).map(|r| r.expect("offset 0 always fits"))
        }
        Err(e) => Err(e),
    };
    let read = opened.and_then(|r| read_lines(r, compressed, Chunk::new(stem, cursor)));
    let ReadResult {
        mut chunk,
        consumed,
    } = match read {
        Ok(r) => r,
        Err(_) => {
            out.files_unreadable += 1;
            return Ok(());
        }
    };

    let stats = &mut out.report.stats;
    let tx = store.conn.transaction()?;
    let source_id = match &prior {
        Some(p) => p.id,
        None => store::ensure_source(&tx, TOOL, &path_str)?,
    };
    if shrank {
        store::reset_source(&tx, source_id)?;
        tx.execute(
            "DELETE FROM codex_cursors WHERE source_id = ?1",
            params![source_id],
        )?;
        stats.files_reset += 1;
    }
    if consumed > 0 {
        let session_id = chunk.session_id().unwrap_or_else(|| stem.to_string());
        let is_new_session: bool = !tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM sessions WHERE tool = ?1 AND session_id = ?2)",
            params![TOOL, session_id],
            |r| r.get(0),
        )?;
        let c = &chunk.cursor;
        store::upsert_session(
            &tx,
            &SessionUpdate {
                tool: TOOL,
                session_id: session_id.clone(),
                source_id,
                is_subagent: c.is_subagent,
                parent_session_id: c.parent_session_id.clone(),
                agent_type: c.agent_type.clone(),
                title: chunk.title.take(),
                cwd: chunk.cwd.take(),
                started_at_ms: chunk.started_at_ms,
                ended_at_ms: chunk.ended_at_ms,
                user_turns: chunk.user_turns,
            },
        )?;
        for r in &chunk.requests {
            if store::upsert_request(&tx, TOOL, &session_id, c.is_subagent, source_id, &r.row)? {
                stats.rows_added += 1;
            }
            tx.prepare_cached(
                "UPDATE requests SET reasoning_tokens = ?3 WHERE tool = ?1 AND request_key = ?2",
            )?
            .execute(params![TOOL, r.row.request_key, r.reasoning_tokens])?;
        }
        tx.execute(
            "INSERT INTO codex_cursors (source_id, state) VALUES (?1, ?2)
             ON CONFLICT(source_id) DO UPDATE SET state = excluded.state",
            params![source_id, serde_json::to_string(&chunk.cursor)?],
        )?;
        stats.files_read += 1;
        stats.bytes_read += consumed;
        stats.bad_lines += chunk.bad_lines;
        stats.sessions_added += is_new_session as u64;
        out.anomalies.add(&chunk.anomalies);
    }
    store::update_source(&tx, source_id, offset + consumed, size, mtime_ms, now_ms())?;
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn usage(i: i64, c: i64, o: i64, r: i64) -> String {
        format!(
            r#"{{"input_tokens":{i},"cached_input_tokens":{c},"cache_write_input_tokens":0,"output_tokens":{o},"reasoning_output_tokens":{r},"total_tokens":{}}}"#,
            i + o
        )
    }

    fn tc(ord: i64, total: (i64, i64, i64, i64), last: (i64, i64, i64, i64)) -> String {
        format!(
            r#"{{"timestamp":"2026-01-01T00:00:{ord:02}.000Z","ordinal":{ord},"type":"event_msg","payload":{{"type":"token_count","info":{{"total_token_usage":{},"last_token_usage":{},"model_context_window":1000}},"rate_limits":null}}}}"#,
            usage(total.0, total.1, total.2, total.3),
            usage(last.0, last.1, last.2, last.3)
        )
    }

    #[test]
    fn injected_blocks() {
        assert!(is_injected(
            "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"
        ));
        assert!(is_injected("  <turn_aborted>x</turn_aborted>"));
        assert!(is_injected("</image>"));
        assert!(is_injected("# AGENTS.md instructions for /x\n..."));
        assert!(!is_injected("<div> is broken, fix it"));
        assert!(!is_injected("fix the build"));
    }

    #[test]
    fn ide_wrapped_title_uses_the_request() {
        let t = "# Files mentioned by the user:\n\n## a.rs: /x/a.rs\n\n## My request for Codex:\nDo the thing\nmore";
        assert_eq!(title_from(t).as_deref(), Some("Do the thing"));
        assert_eq!(title_from("# Selected text:\nonly context"), None);
        let pasted =
            "# Files pasted by the user:\n\n## notes.md\nbody\n\n## My request:\n\nTidy notes";
        assert_eq!(title_from(pasted).as_deref(), Some("Tidy notes"));
        assert_eq!(title_from("\n plain ask \n"), Some("plain ask".into()));
    }

    #[test]
    fn sniffing_skips_irrelevant_lines_only() {
        assert!(!wanted(br#"{"timestamp":"t","ordinal":3,"type":"response_item","payload":{"type":"reasoning","summary":[]}}"#));
        assert!(!wanted(br#"{"timestamp":"t","ordinal":3,"type":"event_msg","payload":{"type":"item_completed"}}"#));
        assert!(!wanted(
            br#"{"timestamp":"t","ordinal":3,"type":"world_state","payload":{}}"#
        ));
        assert!(wanted(
            br#"{"timestamp":"t","ordinal":3,"type":"token_usage_record","payload":{}}"#
        ));
        assert!(wanted(br#"{"timestamp":"t","ordinal":3,"type":"event_msg","payload":{"type":"token_count","info":null}}"#));
        assert!(wanted(br#"{"timestamp":"t","ordinal":3,"type":"response_item","payload":{"type":"message","role":"user"}}"#));
        assert!(!wanted(br#"{"timestamp":"t","ordinal":3,"type":"response_item","payload":{"type":"messages"}}"#));
        assert!(wanted(
            br#"{"timestamp":"t","type":"turn_context","payload":{}}"#
        ));
        // Unknown layout: parse it.
        assert!(wanted(
            br#"{"payload":{"type":"token_count"},"type":"event_msg"}"#
        ));
    }

    #[test]
    fn requests_follow_cumulative_changes_and_use_last_usage() {
        let mut c = Chunk::new("rollout-2026-01-01T00-00-00-x", Cursor::default());
        c.push_line(tc(1, (100, 80, 10, 4), (100, 80, 10, 4)).as_bytes());
        c.push_line(tc(2, (100, 80, 10, 4), (100, 80, 10, 4)).as_bytes()); // repeat
        c.push_line(tc(3, (250, 200, 15, 5), (150, 120, 5, 1)).as_bytes());
        // Counter restarted: delta is negative, last is the call.
        c.push_line(tc(4, (40, 30, 2, 0), (40, 30, 2, 0)).as_bytes());
        assert_eq!(c.requests.len(), 3);
        let r = &c.requests[1];
        assert_eq!(
            (
                r.row.input_tokens,
                r.row.cache_read_input_tokens,
                r.row.output_tokens,
                r.reasoning_tokens
            ),
            (30, 120, 5, 1)
        );
        assert_eq!(r.row.request_key, "rollout-2026-01-01T00-00-00-x#3");
        assert_eq!(c.anomalies.repeated_token_counts, 1);
        assert_eq!(c.anomalies.counter_resets, 1);
        assert_eq!(c.anomalies.usage_mismatch, 1);
        assert_eq!(c.requests[2].row.input_tokens, 10);
    }

    #[test]
    fn usage_records_take_over_from_token_counts() {
        let mut c = Chunk::new("s", Cursor::default());
        c.push_line(br#"{"timestamp":"2026-01-01T00:00:00.000Z","ordinal":0,"type":"session_meta","payload":{"id":"t1"}}"#);
        // Older part of a resumed file: token_count only.
        c.push_line(tc(1, (100, 80, 10, 4), (100, 80, 10, 4)).as_bytes());
        let rec = |ord: i64, thread: &str, resp: &str| {
            format!(
                r#"{{"timestamp":"2026-01-01T00:00:{ord:02}.000Z","ordinal":{ord},"type":"token_usage_record","payload":{{"thread_id":"{thread}","response_id":"{resp}","usage":{}}}}}"#,
                usage(50, 40, 5, 1)
            )
        };
        c.push_line(rec(2, "t1", "resp_1").as_bytes());
        c.push_line(tc(3, (150, 120, 15, 5), (50, 40, 5, 1)).as_bytes());
        c.push_line(rec(4, "other", "resp_x").as_bytes());
        c.push_line(rec(5, "t1", "resp_2").as_bytes());
        // Total did not move after resp_2; the record still counts.
        c.push_line(tc(6, (150, 120, 15, 5), (0, 0, 0, 0)).as_bytes());
        let keys: Vec<&str> = c
            .requests
            .iter()
            .map(|r| r.row.request_key.as_str())
            .collect();
        assert_eq!(keys, ["s#1", "resp_1", "resp_2"]);
        assert_eq!(c.anomalies.foreign_usage_records, 1);
        assert_eq!(c.anomalies.repeated_token_counts, 0);
        assert_eq!(c.cursor.prev_total.unwrap().input_tokens, 150);
    }

    #[test]
    fn cached_above_input_is_clamped_and_counted() {
        let mut c = Chunk::new("s", Cursor::default());
        c.push_line(tc(1, (50, 70, 1, 0), (50, 70, 1, 0)).as_bytes());
        assert_eq!(c.requests[0].row.input_tokens, 0);
        assert_eq!(c.requests[0].row.cache_read_input_tokens, 70);
        assert_eq!(c.anomalies.cached_exceeds_input, 1);
    }

    #[test]
    fn stem_and_fallback_id() {
        let p = Path::new(
            "/a/rollout-2026-01-01T00-00-00-01a10241-0793-74c1-894e-fe7efe02777f_01a10243-077e-7bd2-8edb-55ba0a76e3f6.jsonl.zst",
        );
        let s = rollout_stem(p).unwrap();
        assert!(s.ends_with("e3f6"));
        assert_eq!(
            id_from_stem(s).as_deref(),
            Some("01a10241-0793-74c1-894e-fe7efe02777f")
        );
        assert_eq!(rollout_stem(Path::new("/a/notes.jsonl")), None);
    }
}
