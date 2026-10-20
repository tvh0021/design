//! Incremental scan: each raw log file is read from its stored byte offset to
//! its last complete line, inside one transaction per file.

use crate::claude::{self, Chunk, TranscriptKind};
use crate::labels;
use crate::store::{self, SessionUpdate, Store};
use anyhow::{Context, Result};
use std::fs::File;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::Path;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use walkdir::WalkDir;

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct ScanStats {
    /// Log files found on disk.
    pub files_seen: u64,
    /// Files that had new bytes to read.
    pub files_read: u64,
    pub bytes_read: u64,
    /// New request rows inserted (merges into existing rows are not counted).
    pub rows_added: u64,
    /// Sessions seen for the first time.
    pub sessions_added: u64,
    /// Files re-read from byte 0 because they shrank.
    pub files_reset: u64,
    pub bad_lines: u64,
}

#[derive(Debug, Clone)]
pub struct ScanReport {
    pub stats: ScanStats,
    pub elapsed: Duration,
}

/// Scan every Claude Code transcript under `root` (normally `~/.claude/projects`).
/// A missing root is not an error: there is nothing to read.
pub fn scan_claude(store: &mut Store, root: &Path) -> Result<ScanReport> {
    let start = Instant::now();
    let mut stats = ScanStats::default();
    if root.is_dir() {
        let mut files: Vec<_> = WalkDir::new(root)
            .follow_links(false)
            .into_iter()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_type().is_file())
            .filter_map(|e| {
                let kind = claude::classify(root, e.path())?;
                Some((e.into_path(), kind))
            })
            .collect();
        // Parents before their subagents keeps the store tidy for readers mid-scan.
        files.sort_by(|a, b| a.1.is_subagent.cmp(&b.1.is_subagent).then(a.0.cmp(&b.0)));
        for (path, kind) in files {
            stats.files_seen += 1;
            scan_file(store, &path, &kind, &mut stats)
                .with_context(|| format!("scanning {}", path.display()))?;
        }
    }
    Ok(ScanReport {
        stats,
        elapsed: start.elapsed(),
    })
}

fn scan_file(
    store: &mut Store,
    path: &Path,
    kind: &TranscriptKind,
    stats: &mut ScanStats,
) -> Result<()> {
    let path_str = path.to_string_lossy().into_owned();
    let meta = std::fs::metadata(path)?;
    let size = meta.len();
    let mtime_ms = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as i64);

    let prior = store::source_state(&store.conn, &path_str)?;
    let mut offset = prior.map_or(0, |s| s.offset);
    let shrank = size < offset;
    if !shrank && size == offset {
        return Ok(()); // nothing appended
    }

    let tx = store.conn.transaction()?;
    let source_id = match prior {
        Some(s) => s.id,
        None => store::ensure_source(&tx, claude::TOOL, &path_str)?,
    };
    if shrank {
        store::reset_source(&tx, source_id)?;
        offset = 0;
        stats.files_reset += 1;
    }

    // Read complete lines only; a trailing partial line waits for the next scan.
    let mut file = File::open(path)?;
    file.seek(SeekFrom::Start(offset))?;
    let mut reader = BufReader::with_capacity(1 << 20, file);
    let mut chunk = Chunk::default();
    let mut line = Vec::with_capacity(64 * 1024);
    let mut consumed = 0u64;
    loop {
        line.clear();
        let n = reader.read_until(b'\n', &mut line)?;
        if n == 0 || line.last() != Some(&b'\n') {
            break;
        }
        consumed += n as u64;
        chunk.push_line(&line[..n - 1]);
    }

    if consumed > 0 {
        let is_new_session: bool = !tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM sessions WHERE tool = ?1 AND session_id = ?2)",
            rusqlite::params![claude::TOOL, kind.session_id],
            |r| r.get(0),
        )?;
        let agent_type = if kind.is_subagent && is_new_session {
            claude::read_agent_type(path)
        } else {
            None
        };
        let parent_session_id = if kind.is_subagent {
            kind.parent_session_id
                .clone()
                .or_else(|| chunk.record_session_id.clone())
        } else {
            None
        };
        store::upsert_session(
            &tx,
            &SessionUpdate {
                tool: claude::TOOL,
                session_id: kind.session_id.clone(),
                source_id,
                is_subagent: kind.is_subagent,
                parent_session_id,
                agent_type,
                title: chunk.title.take(),
                cwd: chunk.cwd.take(),
                started_at_ms: chunk.started_at_ms,
                ended_at_ms: chunk.ended_at_ms,
                user_turns: chunk.user_turns,
            },
        )?;
        if let (false, Some(label)) = (kind.is_subagent, chunk.label) {
            labels::set_rule_label(&tx, claude::TOOL, &kind.session_id, label)?;
        }
        for r in &chunk.requests {
            if store::upsert_request(
                &tx,
                claude::TOOL,
                &kind.session_id,
                kind.is_subagent,
                source_id,
                r,
            )? {
                stats.rows_added += 1;
            }
        }
        stats.files_read += 1;
        stats.bytes_read += consumed;
        stats.bad_lines += chunk.bad_lines;
        stats.sessions_added += is_new_session as u64;
    }
    store::update_source(&tx, source_id, offset + consumed, size, mtime_ms, now_ms())?;
    tx.commit()?;
    Ok(())
}

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
