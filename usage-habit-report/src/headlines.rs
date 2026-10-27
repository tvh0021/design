//! Optional harness headlines, refreshed independently of transcript cursors.
use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags, params};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
};

#[derive(Debug)]
pub struct Sources {
    pub codex_state_dir: PathBuf,
    pub codex_session_index: PathBuf,
    pub antigravity_summaries: PathBuf,
}
impl Sources {
    pub fn load(codex_home: &Path) -> Result<Self> {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default();
        let mut out = Self {
            codex_state_dir: codex_home.to_owned(),
            codex_session_index: codex_home.join("session_index.jsonl"),
            antigravity_summaries: home.join(".gemini/antigravity/conversation_summaries.db"),
        };
        for (key, value) in crate::config::load()?.sources {
            let slot = match key.as_str() {
                "codex_state_dir" => &mut out.codex_state_dir,
                "codex_session_index" => &mut out.codex_session_index,
                "antigravity_summaries" => &mut out.antigravity_summaries,
                _ => unreachable!(),
            };
            *slot = value;
        }
        Ok(out)
    }
}
type Titles = HashMap<(String, String), (String, &'static str)>;
pub fn normalize(title: &str) -> Option<String> {
    let title: String = title
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(120)
        .collect();
    (!title.is_empty()).then_some(title)
}
fn open(path: &Path) -> Result<Connection> {
    Ok(Connection::open_with_flags(
        crate::antigravity::sqlite_uri(path, "mode=ro&immutable=1"),
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?)
}
fn codex_db(dir: &Path) -> Result<Titles> {
    let path = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let name = e.file_name();
            let n = name
                .to_str()?
                .strip_prefix("state_")?
                .strip_suffix(".sqlite")?
                .parse::<u64>()
                .ok()?;
            Some((n, e.path()))
        })
        .max_by_key(|(n, _)| *n)
        .context("no Codex title database")?
        .1;
    let c = open(&path)?;
    let mut stmt = c.prepare("SELECT id, name, title FROM threads")?;
    let mut out = Titles::new();
    for row in stmt.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<String>>(1)?,
            r.get::<_, Option<String>>(2)?,
        ))
    })? {
        let (id, name, title) = row?;
        if let Some((title, source)) = name
            .as_deref()
            .and_then(normalize)
            .map(|s| (s, "codex-name"))
            .or_else(|| {
                title
                    .as_deref()
                    .and_then(normalize)
                    .map(|s| (s, "codex-title"))
            })
        {
            out.insert(("codex".into(), id), (title, source));
        }
    }
    Ok(out)
}
fn index(path: &Path) -> Result<Titles> {
    let mut out = Titles::new();
    for line in BufReader::new(std::fs::File::open(path)?).lines() {
        let line = line?;
        let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) else {
            continue;
        };
        if let (Some(id), Some(title)) = (
            v["id"].as_str(),
            v["thread_name"].as_str().and_then(normalize),
        ) {
            out.insert(("codex".into(), id.into()), (title, "codex-name"));
        }
    }
    Ok(out)
}
fn antigravity(path: &Path) -> Result<Titles> {
    let c = open(path)?;
    let mut stmt = c.prepare("SELECT conversation_id, title FROM conversation_summaries")?;
    let mut out = Titles::new();
    for row in stmt.query_map([], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
    })? {
        let (id, title) = row?;
        if let Some(title) = title.as_deref().and_then(normalize) {
            out.insert(("antigravity".into(), id), (title, "antigravity-summary"));
        }
    }
    Ok(out)
}
/// Return an aggregate skipped-source count, without exposing paths or titles.
pub fn refresh(conn: &mut Connection, sources: &Sources) -> Result<usize> {
    let mut skipped = 0;
    let mut titles = match codex_db(&sources.codex_state_dir) {
        Ok(t) => t,
        Err(_) => {
            skipped += 1;
            Titles::new()
        }
    };
    match index(&sources.codex_session_index) {
        Ok(t) => {
            for (key, value) in t {
                if titles.get(&key).is_none_or(|v| v.1 != "codex-name") {
                    titles.insert(key, value);
                }
            }
        }
        Err(_) => skipped += 1,
    }
    match antigravity(&sources.antigravity_summaries) {
        Ok(t) => titles.extend(t),
        Err(_) => skipped += 1,
    }
    let tx = conn.transaction()?;
    // A hand-set title beats every harness headline.
    for row in tx
        .prepare("SELECT tool, session_id, title FROM session_titles")?
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
            ))
        })?
    {
        let (tool, id, title) = row?;
        titles.insert((tool, id), (title, "manual"));
    }
    let rows = tx
        .prepare("SELECT tool, session_id, title, t3_title FROM sessions")?
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<String>>(3)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for (tool, id, raw, t3) in rows {
        let chosen = titles
            .get(&(tool.clone(), id.clone()))
            .cloned()
            .or_else(|| t3.as_deref().and_then(normalize).map(|s| (s, "t3")));
        let (title, source) = match chosen {
            Some((title, source)) => (Some(title), source),
            None => (raw.as_deref().and_then(normalize), "first-line"),
        };
        tx.execute("UPDATE sessions SET display_title = ?3, title_source = ?4 WHERE tool = ?1 AND session_id = ?2 AND (display_title IS NOT ?3 OR title_source IS NOT ?4)", params![tool, id, title, source])?;
    }
    label_from_headlines(&tx)?;
    tx.commit()?;
    Ok(skipped)
}

/// Record a hand-set title and show it at once. Returns the display title it
/// replaced, if any.
pub fn set_manual_title(
    conn: &Connection,
    tool: &str,
    session_id: &str,
    title: &str,
    now_ms: i64,
) -> Result<Option<String>> {
    let Some(title) = normalize(title) else {
        anyhow::bail!("title is empty");
    };
    let before: Option<String> = conn.query_row(
        "SELECT display_title FROM sessions WHERE tool = ?1 AND session_id = ?2",
        params![tool, session_id],
        |r| r.get(0),
    )?;
    conn.execute(
        "INSERT INTO session_titles (tool, session_id, title, set_at) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(tool, session_id) DO UPDATE SET title = excluded.title, set_at = excluded.set_at",
        params![tool, session_id, title, now_ms],
    )?;
    conn.execute(
        "UPDATE sessions SET display_title = ?3, title_source = 'manual'
         WHERE tool = ?1 AND session_id = ?2",
        params![tool, session_id, title],
    )?;
    Ok(before)
}

/// Root sessions (top-level ones, and subagents whose parent is unknown) whose
/// opening prompt was never seen (no user turn in the log, or logs ingested
/// before labels existed) have no task label. When such a session has a
/// headline, classify that instead, under `label_source =
/// 'headline'`, so its requests leave the `unlabelled` column. A hand-set label
/// still wins at query time, and a later rule label is never overwritten here.
fn label_from_headlines(tx: &rusqlite::Transaction) -> Result<usize> {
    let rows = tx
        .prepare(
            "SELECT tool, session_id, display_title FROM sessions
             WHERE label IS NULL AND parent_session_id IS NULL
               AND display_title IS NOT NULL",
        )?
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut n = 0;
    for (tool, id, title) in rows {
        let label = crate::labels::classify(&title);
        n += tx.execute(
            "UPDATE sessions SET label = ?3, label_source = 'headline'
             WHERE tool = ?1 AND session_id = ?2 AND label IS NULL",
            params![tool, id, label],
        )?;
    }
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    #[test]
    fn manual_title_survives_refresh() {
        let mut s = Store::open_in_memory().unwrap();
        s.conn
            .execute(
                "INSERT INTO sessions (tool, session_id, title, display_title, title_source)
                 VALUES ('claude-code', 'aaaa1111', 'first line of prompt', 'first line of prompt', 'first-line')",
                [],
            )
            .unwrap();
        let before =
            set_manual_title(&s.conn, "claude-code", "aaaa1111", "  Short   summary ", 5).unwrap();
        assert_eq!(before.as_deref(), Some("first line of prompt"));
        // Every source missing: the refresh falls back to the opening line
        // for other sessions, but keeps the hand-set title.
        let missing = Sources {
            codex_state_dir: "/nonexistent".into(),
            codex_session_index: "/nonexistent/index.jsonl".into(),
            antigravity_summaries: "/nonexistent/summaries.db".into(),
        };
        refresh(&mut s.conn, &missing).unwrap();
        let (title, source): (String, String) = s
            .conn
            .query_row(
                "SELECT display_title, title_source FROM sessions WHERE session_id = 'aaaa1111'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(title, "Short summary");
        assert_eq!(source, "manual");
        assert!(set_manual_title(&s.conn, "claude-code", "aaaa1111", "   ", 6).is_err());
    }
}
