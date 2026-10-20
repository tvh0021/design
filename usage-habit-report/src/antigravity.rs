//! Antigravity ingest.
//!
//! Each conversation is one SQLite database named `<conversation-id>.db`, in
//! one of two layouts:
//! - native app: `~/.gemini/antigravity/conversations/*.db`
//! - T3 bridge: `~/.t3/userdata/providers/antigravity/*/antigravity-acp/conversations/*.db`,
//!   with a sibling `<id>.meta` JSON holding `cwd` (or `allowed_tools_workspace`).
//!
//! Databases are opened read-only (`mode=ro`), falling back to `immutable=1`
//! when the WAL/SHM sidecars block a read-only open. The scan never writes them.
//!
//! The `steps` table holds one row per step (`idx`, `step_type`, `metadata`,
//! `step_payload`, protobuf blobs). Step type 14 is a user prompt, 15 a model
//! generation. Generation metadata fields used here:
//! - 1: step created (Timestamp), 6: generation start, 7: generation end
//! - 9: usage { 2: uncached input, 3: output incl. thinking, 5: cache read,
//!   7: bot id (native only), 9: thinking }
//! - 24: { 8: selected model, e.g. `gemini-3.8-flash-high` } (T3 bridge)
//!
//! `gen_metadata` has one row per generation: `data` field 1 holds
//! { 4: the same usage message, 19: model }. A generation's model comes from
//! the row whose bot id (native) or usage bytes (T3) match: `measured`. Without
//! a match the step's own selected model is used: also `measured`. Only when a
//! generation names no model at all does the database's first known model
//! stand in: `guessed`.
//!
//! Teamwork child conversations are separate databases. Their
//! `trajectory_metadata_blob` carries the parent conversation id in field 5
//! (also 6) and the agent name in field 4.1; `parent_references` rows, when
//! present, are checked first.

use crate::labels;
use crate::proto;
use crate::scan::{ScanStats, now_ms};
use crate::store::{self, RequestRow, SessionUpdate, Store};
use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags, OptionalExtension, params};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, UNIX_EPOCH};

pub const TOOL: &str = "antigravity";
pub const TITLE_MAX_CHARS: usize = 120;

pub const STEP_USER_PROMPT: i64 = 14;
pub const STEP_GENERATION: i64 = 15;

/// Default conversation folders that exist on this machine: the native app
/// folder and every T3 bridge account folder.
pub fn default_dirs() -> Vec<PathBuf> {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else {
        return Vec::new();
    };
    let mut dirs = vec![home.join(".gemini/antigravity/conversations")];
    let t3 = home.join(".t3/userdata/providers/antigravity");
    if let Ok(rd) = std::fs::read_dir(&t3) {
        let mut accounts: Vec<PathBuf> = rd
            .filter_map(|e| e.ok())
            .map(|e| e.path().join("antigravity-acp/conversations"))
            .collect();
        accounts.sort();
        dirs.extend(accounts);
    }
    dirs.retain(|d| d.is_dir());
    dirs
}

/// `*.db` files directly inside `dir`, sorted.
fn find_dbs(dir: &Path) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = std::fs::read_dir(dir)
        .into_iter()
        .flatten()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "db") && p.is_file())
        .collect();
    out.sort();
    out
}

fn sqlite_uri(path: &Path, query: &str) -> String {
    let mut s = String::from("file:");
    for c in path.to_string_lossy().chars() {
        match c {
            '%' => s.push_str("%25"),
            '?' => s.push_str("%3f"),
            '#' => s.push_str("%23"),
            c => s.push(c),
        }
    }
    s.push('?');
    s.push_str(query);
    s
}

/// Open a conversation database without write access. Returns the connection
/// and whether the `immutable=1` fallback was needed (that mode ignores any
/// WAL content, so the newest steps may be missing).
pub fn open_readonly(path: &Path) -> Result<(Connection, bool)> {
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY
        | OpenFlags::SQLITE_OPEN_URI
        | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let mut last = None;
    for (query, immutable) in [("mode=ro", false), ("mode=ro&immutable=1", true)] {
        let attempt = Connection::open_with_flags(sqlite_uri(path, query), flags).and_then(|c| {
            // Opening can succeed while the first read still fails on the sidecars.
            c.query_row("SELECT 1 FROM steps LIMIT 1", [], |_| Ok(()))
                .optional()?;
            Ok(c)
        });
        match attempt {
            Ok(c) => return Ok((c, immutable)),
            Err(e) => last = Some(e),
        }
    }
    Err(last
        .map(anyhow::Error::from)
        .unwrap_or_else(|| anyhow::anyhow!("no attempt")))
    .with_context(|| format!("opening {} read-only", path.display()))
}

/// Token counts from a generation's usage message (metadata field 9).
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct Usage {
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_read_input_tokens: i64,
    pub reasoning_tokens: Option<i64>,
}

pub fn decode_usage(buf: &[u8]) -> Usage {
    let get = |n| proto::varint(buf, n).map(|v| v as i64);
    Usage {
        input_tokens: get(2).unwrap_or(0),
        output_tokens: get(3).unwrap_or(0),
        cache_read_input_tokens: get(5).unwrap_or(0),
        reasoning_tokens: get(9),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModelSource {
    Measured,
    Guessed,
}

impl ModelSource {
    pub fn as_str(self) -> &'static str {
        match self {
            ModelSource::Measured => "measured",
            ModelSource::Guessed => "guessed",
        }
    }
}

/// One generation step with usage.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Generation {
    pub idx: i64,
    pub ts_ms: i64,
    pub usage: Usage,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub model_source: Option<ModelSource>,
}

/// Everything read from one conversation database.
#[derive(Debug, Clone, Default)]
pub struct Conversation {
    pub parent_session_id: Option<String>,
    pub agent_type: Option<String>,
    pub title: Option<String>,
    /// Task kind of the opening prompt (see `labels`); the prompt is not kept.
    pub label: Option<&'static str>,
    /// Workspace from the trajectory metadata (native layout).
    pub cwd: Option<String>,
    pub started_at_ms: Option<i64>,
    pub ended_at_ms: Option<i64>,
    /// `idx` of every user prompt step.
    pub user_prompt_idxs: Vec<i64>,
    pub generations: Vec<Generation>,
    /// Generation steps with no usage message (cancelled or still running).
    pub generations_without_usage: u64,
    pub max_idx: Option<i64>,
}

impl Conversation {
    /// Session-level model source: `guessed` if any generation's model was
    /// guessed, else `measured` if any was measured, else none.
    pub fn model_source(&self) -> Option<ModelSource> {
        let srcs = self.generations.iter().filter_map(|g| g.model_source);
        srcs.fold(None, |acc, s| match (acc, s) {
            (Some(ModelSource::Guessed), _) | (_, ModelSource::Guessed) => {
                Some(ModelSource::Guessed)
            }
            _ => Some(ModelSource::Measured),
        })
    }
}

/// Rows of a blob column; a missing table (older databases) reads as empty.
fn blobs(conn: &Connection, sql: &str) -> Result<Vec<Vec<u8>>> {
    let mut stmt = match conn.prepare(sql) {
        Ok(s) => s,
        Err(e) if e.to_string().contains("no such table") => return Ok(Vec::new()),
        Err(e) => return Err(e.into()),
    };
    let rows = stmt
        .query_map([], |r| r.get::<_, Option<Vec<u8>>>(0))?
        .filter_map(|r| r.transpose())
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(rows)
}

fn is_uuid(b: &[u8]) -> bool {
    b.len() == 36
        && b.iter().enumerate().all(|(i, &c)| match i {
            8 | 13 | 18 | 23 => c == b'-',
            _ => c.is_ascii_hexdigit(),
        })
}

/// Model names as they appear in Antigravity metadata.
fn looks_like_model(b: &[u8]) -> bool {
    (5..=64).contains(&b.len())
        && ["gemini-", "claude-", "gpt-"]
            .iter()
            .any(|p| b.len() > p.len() && b.starts_with(p.as_bytes()))
        && b.iter()
            .all(|&c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"-._".contains(&c))
}

/// Selected-model strings such as `gemini-3.8-flash-high` are the generation
/// model plus a thinking level. Split off the level when the base is known.
fn split_selected<'a>(selected: &'a str, known: &[String]) -> (&'a str, Option<&'a str>) {
    known
        .iter()
        .filter(|m| !m.is_empty())
        .find_map(|m| {
            let rest = selected.strip_prefix(m.as_str())?;
            if rest.is_empty() {
                Some((&selected[..m.len()], None))
            } else {
                let lvl = rest.strip_prefix('-')?;
                Some((&selected[..m.len()], Some(lvl)))
            }
        })
        .unwrap_or((selected, None))
}

/// gen_metadata lookups: bot id -> model, usage bytes -> model, plus the
/// distinct models in order of first appearance.
#[derive(Default)]
struct ModelIndex {
    by_bot: HashMap<String, String>,
    by_usage: HashMap<Vec<u8>, String>,
    models: Vec<String>,
}

impl ModelIndex {
    fn load(conn: &Connection) -> Result<Self> {
        let mut ix = ModelIndex::default();
        for data in blobs(conn, "SELECT data FROM gen_metadata ORDER BY idx")? {
            let Some(g) = proto::bytes(&data, 1) else {
                continue;
            };
            let Some(model) = proto::string(g, 19).filter(|m| !m.is_empty()) else {
                continue;
            };
            if !ix.models.iter().any(|m| m == model) {
                ix.models.push(model.to_string());
            }
            if let Some(usage) = proto::bytes(g, 4) {
                if let Some(bot) = proto::string(usage, 7).filter(|b| !b.is_empty()) {
                    ix.by_bot.insert(bot.to_string(), model.to_string());
                }
                ix.by_usage.insert(usage.to_vec(), model.to_string());
            }
        }
        Ok(ix)
    }
}

/// Database-wide model to fall back on: the first gen_metadata model, else the
/// first model-like string in the executor or trajectory metadata.
fn fallback_model(conn: &Connection, ix: &ModelIndex) -> Result<Option<String>> {
    if let Some(m) = ix.models.first() {
        return Ok(Some(m.clone()));
    }
    for sql in [
        "SELECT data FROM executor_metadata ORDER BY idx",
        "SELECT data FROM trajectory_metadata_blob",
    ] {
        for data in blobs(conn, sql)? {
            let found = proto::walk_bytes(&data, 16, &mut |b| {
                looks_like_model(b).then(|| String::from_utf8_lossy(b).into_owned())
            });
            if found.is_some() {
                return Ok(found);
            }
        }
    }
    Ok(None)
}

/// Text of a user prompt step.
fn prompt_text(payload: &[u8]) -> Option<String> {
    let body = proto::bytes(payload, 19)?;
    Some(
        match proto::string(body, 2).filter(|s| !s.trim().is_empty()) {
            Some(s) => s.to_string(),
            None => proto::fields(body)
                .filter(|(n, _)| *n == 3)
                .filter_map(|(_, v)| match v {
                    proto::Value::Bytes(b) => proto::string(b, 1),
                    _ => None,
                })
                .collect::<String>(),
        },
    )
}

/// First line of the prompt text, at most `TITLE_MAX_CHARS` characters.
fn title_from_text(text: &str) -> Option<String> {
    let line = text.lines().map(str::trim).find(|l| !l.is_empty())?;
    Some(line.chars().take(TITLE_MAX_CHARS).collect())
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let hex = |c: u8| (c as char).to_digit(16).map(|d| d as u8);
        if b[i] == b'%'
            && i + 2 < b.len()
            && let (Some(h), Some(l)) = (hex(b[i + 1]), hex(b[i + 2]))
        {
            out.push(h << 4 | l);
            i += 3;
            continue;
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Parent id, agent name, and workspace from the trajectory metadata.
fn read_lineage(conn: &Connection, session_id: &str, c: &mut Conversation) -> Result<()> {
    let not_self = |b: &[u8]| is_uuid(b) && b != session_id.as_bytes();
    for data in blobs(conn, "SELECT data FROM parent_references ORDER BY idx")? {
        if let Some(p) = proto::walk_bytes(&data, 8, &mut |b| {
            not_self(b).then(|| String::from_utf8_lossy(b).into_owned())
        }) {
            c.parent_session_id = Some(p);
            break;
        }
    }
    for data in blobs(conn, "SELECT data FROM trajectory_metadata_blob")? {
        if c.parent_session_id.is_none() {
            c.parent_session_id = [5, 6]
                .iter()
                .filter_map(|&n| proto::bytes(&data, n))
                .find(|b| not_self(b))
                .map(|b| String::from_utf8_lossy(b).into_owned());
        }
        if c.agent_type.is_none() {
            c.agent_type = proto::path(&data, &[4])
                .and_then(|a| proto::string(a, 1))
                .filter(|s| !s.is_empty())
                .map(str::to_string);
        }
        if c.cwd.is_none() {
            c.cwd = proto::string(&data, 7)
                .and_then(|u| u.strip_prefix("file://"))
                .filter(|p| !p.is_empty())
                .map(percent_decode);
        }
    }
    Ok(())
}

/// Read one conversation database. Only token counts, timestamps, model
/// names, lineage, and the title line are kept.
pub fn read_conversation(conn: &Connection, session_id: &str) -> Result<Conversation> {
    let mut c = Conversation::default();
    read_lineage(conn, session_id, &mut c)?;
    let ix = ModelIndex::load(conn)?;
    let mut fallback: Option<Option<String>> = None;

    let mut stmt =
        conn.prepare("SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx")?;
    let mut rows = stmt.query([])?;
    while let Some(r) = rows.next()? {
        let idx: i64 = r.get(0)?;
        let step_type: i64 = r.get(1)?;
        let meta: Option<Vec<u8>> = r.get(2)?;
        let meta = meta.unwrap_or_default();
        c.max_idx = Some(c.max_idx.map_or(idx, |m| m.max(idx)));
        let created = proto::bytes(&meta, 1).and_then(proto::timestamp_ms);
        let ended = proto::bytes(&meta, 7).and_then(proto::timestamp_ms);
        for t in [created, ended].into_iter().flatten() {
            c.started_at_ms = Some(c.started_at_ms.map_or(t, |s| s.min(t)));
            c.ended_at_ms = Some(c.ended_at_ms.map_or(t, |e| e.max(t)));
        }
        match step_type {
            STEP_USER_PROMPT => {
                c.user_prompt_idxs.push(idx);
                if c.title.is_none() {
                    let payload: Option<Vec<u8>> = r.get(3)?;
                    let text = payload.as_deref().and_then(prompt_text);
                    c.title = text.as_deref().and_then(title_from_text);
                    if let (Some(_), Some(text)) = (&c.title, &text) {
                        c.label = Some(labels::classify(text));
                    }
                }
            }
            STEP_GENERATION => {
                let Some(usage_buf) = proto::bytes(&meta, 9) else {
                    c.generations_without_usage += 1;
                    continue;
                };
                let usage = decode_usage(usage_buf);
                let selected = proto::path(&meta, &[24])
                    .and_then(|s| proto::string(s, 8))
                    .filter(|s| !s.is_empty());
                let joined = proto::string(usage_buf, 7)
                    .and_then(|bot| ix.by_bot.get(bot))
                    .or_else(|| ix.by_usage.get(usage_buf));
                let (model, effort, source) = if let Some(m) = joined {
                    let effort = selected.and_then(|s| {
                        split_selected(s, std::slice::from_ref(m))
                            .1
                            .map(str::to_string)
                    });
                    (Some(m.clone()), effort, Some(ModelSource::Measured))
                } else if let Some(s) = selected {
                    let (m, e) = split_selected(s, &ix.models);
                    (
                        Some(m.to_string()),
                        e.map(str::to_string),
                        Some(ModelSource::Measured),
                    )
                } else {
                    let fb = match &fallback {
                        Some(f) => f.clone(),
                        None => {
                            let f = fallback_model(conn, &ix)?;
                            fallback = Some(f.clone());
                            f
                        }
                    };
                    let src = fb.as_ref().map(|_| ModelSource::Guessed);
                    (fb, None, src)
                };
                let ts_ms = proto::bytes(&meta, 6)
                    .and_then(proto::timestamp_ms)
                    .or(created)
                    .unwrap_or(0);
                c.generations.push(Generation {
                    idx,
                    ts_ms,
                    usage,
                    model,
                    effort,
                    model_source: source,
                });
            }
            _ => {}
        }
    }
    Ok(c)
}

/// `cwd` (or `allowed_tools_workspace`) from the T3 bridge's `<id>.meta`.
fn read_meta_cwd(db: &Path) -> Option<String> {
    let text = std::fs::read_to_string(db.with_extension("meta")).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    ["cwd", "allowed_tools_workspace"]
        .iter()
        .find_map(|k| v.get(k)?.as_str().filter(|s| !s.is_empty()))
        .map(str::to_string)
}

/// Combined size and latest mtime of the database and its WAL: a write can
/// land in either.
fn fingerprint(db: &Path) -> Result<(u64, Option<i64>)> {
    let mut size = 0u64;
    let mut mtime: Option<i64> = None;
    let mut wal = db.as_os_str().to_owned();
    wal.push("-wal");
    for (p, required) in [(db.to_path_buf(), true), (PathBuf::from(wal), false)] {
        let meta = match std::fs::metadata(&p) {
            Ok(m) => m,
            Err(_) if !required => continue,
            Err(e) => return Err(e).with_context(|| format!("stat {}", p.display())),
        };
        size += meta.len();
        let m = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_millis() as i64);
        mtime = mtime.max(m);
    }
    Ok((size, mtime))
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct AntigravityStats {
    /// `files_*` count databases; `bytes_read` is database + WAL bytes of the
    /// databases that were read; `files_reset` counts databases whose step
    /// count fell below what was stored.
    pub scan: ScanStats,
    /// Databases that needed the `immutable=1` fallback.
    pub immutable_opens: u64,
    /// Databases that could not be opened or read; retried next scan.
    pub unreadable: u64,
    pub generations_without_usage: u64,
}

#[derive(Debug, Clone)]
pub struct AntigravityReport {
    pub stats: AntigravityStats,
    pub elapsed: Duration,
}

/// Scan every conversation database in `dirs`. Missing folders are skipped.
///
/// A database whose size and mtime (database + WAL) match the stored values
/// is not opened. Otherwise all its steps are read again, because SQLite
/// rewrites rows in place (a generation gains its usage after it finishes).
/// Requests are keyed `<conversation-id>:<step idx>`, so re-reads merge.
/// The source row's `offset` holds the next unseen step idx; only prompts at
/// or past it add user turns, and a database whose steps fell below it is
/// reset and re-read.
pub fn scan_antigravity(store: &mut Store, dirs: &[PathBuf]) -> Result<AntigravityReport> {
    let start = Instant::now();
    let mut st = AntigravityStats::default();
    for dir in dirs {
        for db in find_dbs(dir) {
            st.scan.files_seen += 1;
            scan_db(store, &db, &mut st).with_context(|| format!("scanning {}", db.display()))?;
        }
    }
    Ok(AntigravityReport {
        stats: st,
        elapsed: start.elapsed(),
    })
}

fn scan_db(store: &mut Store, db: &Path, st: &mut AntigravityStats) -> Result<()> {
    let Some(session_id) = db.file_stem().and_then(|s| s.to_str()).map(str::to_string) else {
        return Ok(());
    };
    let path_str = db.to_string_lossy().into_owned();
    let (size, mtime_ms) = fingerprint(db)?;
    let prior: Option<(i64, i64, i64, Option<i64>)> = store
        .conn
        .query_row(
            "SELECT id, offset, size, mtime_ms FROM sources WHERE path = ?1",
            params![path_str],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()?;
    if let Some((_, _, psize, pmtime)) = prior
        && psize as u64 == size
        && pmtime == mtime_ms
    {
        return Ok(());
    }

    let conv = match open_readonly(db).and_then(|(conn, immutable)| {
        let c = read_conversation(&conn, &session_id)?;
        Ok((c, immutable))
    }) {
        Ok((c, immutable)) => {
            st.immutable_opens += immutable as u64;
            c
        }
        Err(e) => {
            eprintln!("antigravity: skipping {}: {e:#}", db.display());
            st.unreadable += 1;
            return Ok(());
        }
    };
    let next_idx = conv.max_idx.map_or(0, |m| m + 1);

    let tx = store.conn.transaction()?;
    let source_id = match prior {
        Some((id, ..)) => id,
        None => store::ensure_source(&tx, TOOL, &path_str)?,
    };
    let mut offset = prior.map_or(0, |p| p.1);
    if offset > next_idx {
        store::reset_source(&tx, source_id)?;
        offset = 0;
        st.scan.files_reset += 1;
    }

    let is_new_session: bool = !tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM sessions WHERE tool = ?1 AND session_id = ?2)",
        params![TOOL, session_id],
        |r| r.get(0),
    )?;
    let is_subagent = conv.parent_session_id.is_some();
    store::upsert_session(
        &tx,
        &SessionUpdate {
            tool: TOOL,
            session_id: session_id.clone(),
            source_id,
            is_subagent,
            parent_session_id: conv.parent_session_id.clone(),
            agent_type: conv.agent_type.clone(),
            title: conv.title.clone(),
            cwd: read_meta_cwd(db).or_else(|| conv.cwd.clone()),
            started_at_ms: conv.started_at_ms,
            ended_at_ms: conv.ended_at_ms,
            user_turns: conv
                .user_prompt_idxs
                .iter()
                .filter(|&&i| i >= offset)
                .count() as i64,
        },
    )?;
    if let (false, Some(label)) = (is_subagent, conv.label) {
        labels::set_rule_label(&tx, TOOL, &session_id, label)?;
    }
    tx.execute(
        "UPDATE sessions SET model_source = ?3 WHERE tool = ?1 AND session_id = ?2",
        params![
            TOOL,
            session_id,
            conv.model_source().map(ModelSource::as_str)
        ],
    )?;
    for g in &conv.generations {
        let key = format!("{session_id}:{}", g.idx);
        let row = RequestRow {
            request_key: key.clone(),
            ts_ms: g.ts_ms,
            model: g.model.clone(),
            effort: g.effort.clone(),
            input_tokens: g.usage.input_tokens,
            cache_read_input_tokens: g.usage.cache_read_input_tokens,
            cache_creation_input_tokens: 0,
            output_tokens: g.usage.output_tokens,
        };
        if store::upsert_request(&tx, TOOL, &session_id, is_subagent, source_id, &row)? {
            st.scan.rows_added += 1;
        }
        if let Some(r) = g.usage.reasoning_tokens {
            tx.prepare_cached(
                "UPDATE requests SET reasoning_tokens = MAX(COALESCE(reasoning_tokens, 0), ?3)
                 WHERE tool = ?1 AND request_key = ?2",
            )?
            .execute(params![TOOL, key, r])?;
        }
    }
    store::update_source(&tx, source_id, next_idx as u64, size, mtime_ms, now_ms())?;
    tx.commit()?;

    st.scan.files_read += 1;
    st.scan.bytes_read += size;
    st.scan.sessions_added += is_new_session as u64;
    st.generations_without_usage += conv.generations_without_usage;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uuid_shape() {
        assert!(is_uuid(b"0a0a0a0a-1b1b-4c4c-8d8d-0e0e0e0e0e0e"));
        assert!(!is_uuid(b"0a0a0a0a-1b1b-4c4c-8d8d-0e0e0e0e0e0"));
        assert!(!is_uuid(b"0a0a0a0a_1b1b-4c4c-8d8d-0e0e0e0e0e0e"));
    }

    #[test]
    fn selected_model_split() {
        let known = vec!["gemini-9-flash".to_string()];
        assert_eq!(
            split_selected("gemini-9-flash-high", &known),
            ("gemini-9-flash", Some("high"))
        );
        assert_eq!(
            split_selected("gemini-9-flash", &known),
            ("gemini-9-flash", None)
        );
        assert_eq!(
            split_selected("gemini-9-flashy", &known),
            ("gemini-9-flashy", None)
        );
        assert_eq!(
            split_selected("gemini-pro-agent", &known),
            ("gemini-pro-agent", None)
        );
    }

    #[test]
    fn model_like() {
        assert!(looks_like_model(b"gemini-9-flash-high"));
        assert!(looks_like_model(b"claude-example-7"));
        assert!(!looks_like_model(b"Gemini is a model"));
        assert!(!looks_like_model(b"gemini-"));
    }

    #[test]
    fn decodes_file_uri() {
        assert_eq!(percent_decode("/tmp/a%20b/c"), "/tmp/a b/c");
        assert_eq!(percent_decode("/tmp/100%"), "/tmp/100%");
        assert_eq!(percent_decode("/tmp/%zz"), "/tmp/%zz");
    }

    #[test]
    fn usage_fields() {
        // { 2: 300, 3: 40, 5: 1000, 9: 25 }
        let buf = [0x10, 0xac, 0x02, 0x18, 0x28, 0x28, 0xe8, 0x07, 0x48, 0x19];
        assert_eq!(
            decode_usage(&buf),
            Usage {
                input_tokens: 300,
                output_tokens: 40,
                cache_read_input_tokens: 1000,
                reasoning_tokens: Some(25),
            }
        );
    }
}
