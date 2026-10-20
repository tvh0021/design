//! Claude Code transcript parsing.
//!
//! Layout under `~/.claude/projects/`:
//! - `<project>/<session-uuid>.jsonl` is a top-level session; its id is the file stem.
//! - `<project>/<session-uuid>/subagents/agent-<id>.jsonl` is a subagent transcript.
//!   Its records carry `sessionId` = the parent session uuid (same as the folder
//!   name) plus an `agentId`; a sibling `agent-<id>.meta.json` holds `agentType`.
//!
//! Claude Code writes one `assistant` record per content block, each repeating
//! the message's `usage` (output counts grow while streaming). Requests are
//! therefore keyed by `message.id` and merged by MAX.

use crate::labels;
use crate::store::RequestRow;
use serde::Deserialize;
use serde_json::value::RawValue;
use std::borrow::Cow;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

pub const TOOL: &str = "claude-code";
pub const TITLE_MAX_CHARS: usize = 120;

/// User records that are not typed by a person: slash-command plumbing and
/// harness notifications. They never become the title and do not count as turns.
const INJECTED_PREFIXES: &[&str] = &[
    "<local-command",
    "<command-name",
    "<command-message",
    "<task-notification",
];

/// Client-generated assistant messages (errors, interrupts); not API calls.
const SYNTHETIC_MODEL: &str = "<synthetic>";

/// Where a transcript sits in the session tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptKind {
    pub session_id: String,
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
}

/// Classify a `.jsonl` path found under the projects root.
pub fn classify(root: &Path, path: &Path) -> Option<TranscriptKind> {
    if path.extension()? != "jsonl" {
        return None;
    }
    let stem = path.file_stem()?.to_str()?.to_string();
    let rel = path.strip_prefix(root).ok()?;
    let depth = rel.components().count();
    if depth <= 2 {
        // <project>/<session>.jsonl (or a stray file directly under root)
        return Some(TranscriptKind {
            session_id: stem,
            is_subagent: false,
            parent_session_id: None,
        });
    }
    // Nested: `<project>/<parent-session>/subagents/agent-<id>.jsonl`. The
    // folder under the project is the parent session id; the records'
    // `sessionId` carries the same value.
    let parent = rel
        .components()
        .nth(1)
        .and_then(|c| c.as_os_str().to_str())
        .map(str::to_string);
    Some(TranscriptKind {
        session_id: stem,
        is_subagent: true,
        parent_session_id: parent,
    })
}

/// `agentType` from the `agent-<id>.meta.json` next to a subagent transcript.
pub fn read_agent_type(transcript: &Path) -> Option<String> {
    #[derive(Deserialize)]
    struct Meta {
        #[serde(rename = "agentType")]
        agent_type: Option<String>,
    }
    let meta: PathBuf = transcript.with_extension("meta.json");
    let bytes = std::fs::read(meta).ok()?;
    serde_json::from_slice::<Meta>(&bytes).ok()?.agent_type
}

#[derive(Deserialize)]
struct Record<'a> {
    #[serde(rename = "type", borrow)]
    kind: Option<Cow<'a, str>>,
    #[serde(borrow)]
    timestamp: Option<Cow<'a, str>>,
    cwd: Option<String>,
    #[serde(rename = "sessionId")]
    session_id: Option<String>,
    effort: Option<serde_json::Value>,
    #[serde(rename = "isMeta")]
    is_meta: Option<bool>,
    #[serde(rename = "isCompactSummary")]
    is_compact_summary: Option<bool>,
    uuid: Option<String>,
    #[serde(borrow)]
    message: Option<Message<'a>>,
}

#[derive(Deserialize)]
struct Message<'a> {
    id: Option<String>,
    model: Option<String>,
    usage: Option<Usage>,
    #[serde(borrow)]
    content: Option<&'a RawValue>,
}

#[derive(Deserialize, Default)]
struct Usage {
    input_tokens: Option<i64>,
    cache_read_input_tokens: Option<i64>,
    cache_creation_input_tokens: Option<i64>,
    output_tokens: Option<i64>,
}

#[derive(Deserialize)]
struct Block {
    #[serde(rename = "type")]
    kind: Option<String>,
    text: Option<String>,
}

/// Everything learned from a run of lines of one transcript.
#[derive(Debug, Default)]
pub struct Chunk {
    /// Requests in first-seen order, already merged by `request_key`.
    pub requests: Vec<RequestRow>,
    index: HashMap<String, usize>,
    pub title: Option<String>,
    /// Task kind of the opening prompt (see `labels`); the prompt is not kept.
    pub label: Option<&'static str>,
    pub cwd: Option<String>,
    /// `sessionId` field seen in the records (parent id for subagents).
    pub record_session_id: Option<String>,
    pub started_at_ms: Option<i64>,
    pub ended_at_ms: Option<i64>,
    pub user_turns: i64,
    pub bad_lines: u64,
}

impl Chunk {
    /// Feed one complete line (without the trailing newline).
    pub fn push_line(&mut self, line: &[u8]) {
        let line = trim_ascii(line);
        if line.is_empty() {
            return;
        }
        let rec: Record = match serde_json::from_slice(line) {
            Ok(r) => r,
            Err(_) => {
                self.bad_lines += 1;
                return;
            }
        };
        let ts_ms = rec.timestamp.as_deref().and_then(parse_ts_ms);
        match rec.kind.as_deref() {
            Some("assistant") => self.push_assistant(rec, ts_ms),
            Some("user") => self.push_user(rec, ts_ms),
            _ => {}
        }
    }

    fn touch(&mut self, ts_ms: Option<i64>, cwd: Option<String>, sid: Option<String>) {
        if let Some(t) = ts_ms {
            self.started_at_ms = Some(self.started_at_ms.map_or(t, |s| s.min(t)));
            self.ended_at_ms = Some(self.ended_at_ms.map_or(t, |e| e.max(t)));
        }
        if self.cwd.is_none() {
            self.cwd = cwd.filter(|c| !c.is_empty());
        }
        if self.record_session_id.is_none() {
            self.record_session_id = sid;
        }
    }

    fn push_assistant(&mut self, rec: Record, ts_ms: Option<i64>) {
        let Some(msg) = rec.message else { return };
        if msg.model.as_deref() == Some(SYNTHETIC_MODEL) {
            return;
        }
        let Some(usage) = msg.usage else { return };
        // A request needs a time; a usage record without one is unusable.
        let Some(ts) = ts_ms else {
            self.bad_lines += 1;
            return;
        };
        let Some(key) = msg.id.or(rec.uuid) else {
            self.bad_lines += 1;
            return;
        };
        self.touch(ts_ms, rec.cwd, rec.session_id);
        let row = RequestRow {
            request_key: key,
            ts_ms: ts,
            model: msg.model.filter(|m| !m.is_empty()),
            effort: rec.effort.and_then(|v| v.as_str().map(str::to_string)),
            input_tokens: usage.input_tokens.unwrap_or(0),
            cache_read_input_tokens: usage.cache_read_input_tokens.unwrap_or(0),
            cache_creation_input_tokens: usage.cache_creation_input_tokens.unwrap_or(0),
            output_tokens: usage.output_tokens.unwrap_or(0),
        };
        match self.index.get(&row.request_key) {
            Some(&i) => merge_max(&mut self.requests[i], row),
            None => {
                self.index
                    .insert(row.request_key.clone(), self.requests.len());
                self.requests.push(row);
            }
        }
    }

    fn push_user(&mut self, rec: Record, ts_ms: Option<i64>) {
        let is_meta = rec.is_meta == Some(true) || rec.is_compact_summary == Some(true);
        let content = rec.message.as_ref().and_then(|m| m.content);
        self.touch(ts_ms, rec.cwd, rec.session_id);
        if is_meta {
            return;
        }
        let Some(text) = content.and_then(prompt_text) else {
            return;
        };
        let trimmed = text.trim_start();
        if INJECTED_PREFIXES.iter().any(|p| trimmed.starts_with(p)) {
            return;
        }
        self.user_turns += 1;
        if self.title.is_none() {
            self.title = make_title(trimmed);
            if self.title.is_some() {
                self.label = Some(labels::classify(trimmed));
            }
        }
    }
}

/// Text a person typed, if this user content is a prompt (not a tool result).
fn prompt_text(raw: &RawValue) -> Option<String> {
    let s = raw.get();
    match s.as_bytes().first()? {
        b'"' => serde_json::from_str::<String>(s).ok(),
        b'[' => {
            let blocks: Vec<Block> = serde_json::from_str(s).ok()?;
            if blocks
                .iter()
                .any(|b| b.kind.as_deref() == Some("tool_result"))
            {
                return None;
            }
            blocks
                .into_iter()
                .find(|b| b.kind.as_deref() == Some("text"))
                .and_then(|b| b.text)
        }
        _ => None,
    }
}

/// First non-empty line, cut to `TITLE_MAX_CHARS` characters.
pub fn make_title(text: &str) -> Option<String> {
    let line = text.lines().map(str::trim).find(|l| !l.is_empty())?;
    Some(line.chars().take(TITLE_MAX_CHARS).collect())
}

fn merge_max(a: &mut RequestRow, b: RequestRow) {
    a.input_tokens = a.input_tokens.max(b.input_tokens);
    a.cache_read_input_tokens = a.cache_read_input_tokens.max(b.cache_read_input_tokens);
    a.cache_creation_input_tokens = a
        .cache_creation_input_tokens
        .max(b.cache_creation_input_tokens);
    a.output_tokens = a.output_tokens.max(b.output_tokens);
    if a.model.is_none() {
        a.model = b.model;
    }
    if a.effort.is_none() {
        a.effort = b.effort;
    }
}

pub fn parse_ts_ms(s: &str) -> Option<i64> {
    chrono::DateTime::parse_from_rfc3339(s)
        .ok()
        .map(|d| d.timestamp_millis())
}

fn trim_ascii(mut b: &[u8]) -> &[u8] {
    while let [rest @ .., last] = b {
        if last.is_ascii_whitespace() {
            b = rest;
        } else {
            break;
        }
    }
    b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn title_is_first_nonempty_line_capped() {
        assert_eq!(
            make_title("\n  hello world \nsecond"),
            Some("hello world".into())
        );
        let long = "x".repeat(300);
        assert_eq!(make_title(&long).unwrap().chars().count(), TITLE_MAX_CHARS);
        assert_eq!(make_title("  \n "), None);
    }

    #[test]
    fn streaming_duplicates_merge_by_max() {
        let mut c = Chunk::default();
        let l1 = br#"{"type":"assistant","timestamp":"2026-01-01T00:00:00.000Z","message":{"id":"m1","model":"claude-x","usage":{"input_tokens":3,"cache_read_input_tokens":10,"cache_creation_input_tokens":5,"output_tokens":2}}}"#;
        let l2 = br#"{"type":"assistant","timestamp":"2026-01-01T00:00:01.000Z","message":{"id":"m1","model":"claude-x","usage":{"input_tokens":3,"cache_read_input_tokens":10,"cache_creation_input_tokens":5,"output_tokens":40}}}"#;
        c.push_line(l1);
        c.push_line(l2);
        assert_eq!(c.requests.len(), 1);
        assert_eq!(c.requests[0].output_tokens, 40);
        assert_eq!(c.requests[0].cache_creation_input_tokens, 5);
    }

    #[test]
    fn injected_user_records_are_not_prompts() {
        let mut c = Chunk::default();
        c.push_line(br#"{"type":"user","message":{"role":"user","content":"<command-name>/clear</command-name>"}}"#);
        c.push_line(br#"{"type":"user","message":{"role":"user","content":"<local-command-stdout></local-command-stdout>"}}"#);
        c.push_line(br#"{"type":"user","isMeta":true,"message":{"role":"user","content":"meta"}}"#);
        c.push_line(br#"{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"x"}]}}"#);
        assert_eq!(c.user_turns, 0);
        assert_eq!(c.title, None);
        c.push_line(br#"{"type":"user","message":{"role":"user","content":[{"type":"text","text":"real ask\nmore"}]}}"#);
        assert_eq!(c.user_turns, 1);
        assert_eq!(c.title.as_deref(), Some("real ask"));
    }

    #[test]
    fn bad_lines_are_counted_not_fatal() {
        let mut c = Chunk::default();
        c.push_line(b"{not json");
        c.push_line(b"");
        assert_eq!(c.bad_lines, 1);
    }
}
