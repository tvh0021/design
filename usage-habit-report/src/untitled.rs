//! Highlighted sessions that still show a raw opening line.
//!
//! The dashboard's session lists (heaviest fan-outs, longest, costliest) are
//! recomputed for every window and tool page, so the set of sessions a reader
//! can see changes as sessions age in and out of the windows. `query untitled`
//! walks every page with the same functions the pages use and reports the
//! sessions whose title is still the opening prompt, with the transcript to
//! summarize. A hand-set title (`title`) removes a session from the list.

use anyhow::Result;
use rusqlite::{Connection, params};
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use crate::serve::data::{self, Window};

/// Tool pages the dashboard offers beside the all-tools page.
const TOOLS: [&str; 3] = ["claude-code", "codex", "antigravity"];

/// Title sources that are only the opening prompt. Everything else is a real
/// headline from the harness, T3, or `title`.
const WEAK_SOURCES: [&str; 2] = ["first-line", "codex-title"];

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Untitled {
    pub tool: String,
    pub session_id: String,
    /// `first-line` or `codex-title`.
    pub title_source: Option<String>,
    /// The display title as the dashboard shows it now.
    pub title: Option<String>,
    /// Raw log the session was read from; null when the source row is gone.
    pub path: Option<String>,
    /// A plain-text transcript to summarize from. For Antigravity this is the
    /// `brain/<id>/.system_generated/logs/transcript.jsonl` beside the
    /// conversation database, when it exists; otherwise the same as `path`.
    pub transcript: Option<String>,
    /// Dashboard pages the session is highlighted on: `4w`, `1w/codex`, ...
    pub pages: Vec<String>,
}

/// Every session highlighted on any dashboard page whose title is still the
/// opening prompt, most-highlighted first.
pub fn untitled(conn: &Connection, now_ms: i64) -> Result<Vec<Untitled>> {
    let mut pages: BTreeMap<(String, String), Vec<String>> = BTreeMap::new();
    for window in Window::ALL {
        let since = window.since_ms(now_ms);
        for tool in std::iter::once(None).chain(TOOLS.iter().map(|t| Some(*t))) {
            let page = match tool {
                Some(t) => format!("{}/{t}", window.key()),
                None => window.key().to_string(),
            };
            let agents = crate::agents::agents_report_for(conn, since, tool, now_ms)?;
            let lists = data::session_lists(conn, since, tool)?;
            let keys = agents
                .heaviest_fanouts
                .iter()
                .map(|s| (s.tool.clone(), s.session_id.clone()))
                .chain(
                    lists
                        .longest
                        .iter()
                        .map(|s| (s.tool.clone(), s.session_id.clone())),
                )
                .chain(
                    lists
                        .costliest
                        .iter()
                        .map(|s| (s.tool.clone(), s.session_id.clone())),
                );
            for key in keys {
                let seen = pages.entry(key).or_default();
                if !seen.contains(&page) {
                    seen.push(page.clone());
                }
            }
        }
    }
    let mut stmt = conn.prepare(
        "SELECT s.title_source, COALESCE(s.display_title, s.title), src.path
         FROM sessions s LEFT JOIN sources src ON src.id = s.source_id
         WHERE s.tool = ?1 AND s.session_id = ?2",
    )?;
    let mut out = Vec::new();
    for ((tool, session_id), pages) in pages {
        let (title_source, title, path): (Option<String>, Option<String>, Option<String>) = stmt
            .query_row(params![tool, session_id], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })?;
        if !title_source
            .as_deref()
            .is_some_and(|s| WEAK_SOURCES.contains(&s))
        {
            continue;
        }
        let transcript = path
            .as_deref()
            .map(|p| transcript_path(&tool, &session_id, Path::new(p)))
            .map(|p| p.to_string_lossy().into_owned());
        out.push(Untitled {
            tool,
            session_id,
            title_source,
            title,
            path,
            transcript,
            pages,
        });
    }
    out.sort_by(|a, b| {
        b.pages
            .len()
            .cmp(&a.pages.len())
            .then_with(|| (&a.tool, &a.session_id).cmp(&(&b.tool, &b.session_id)))
    });
    Ok(out)
}

/// The readable transcript for a source path. Antigravity's `conversations/
/// <id>.db` is protobuf; the harness also writes `brain/<id>/.system_generated/
/// logs/transcript.jsonl`, used when present.
fn transcript_path(tool: &str, session_id: &str, path: &Path) -> PathBuf {
    if tool == "antigravity"
        && let Some(root) = path.parent().and_then(Path::parent)
    {
        let brain = root
            .join("brain")
            .join(session_id)
            .join(".system_generated")
            .join("logs")
            .join("transcript.jsonl");
        if brain.is_file() {
            return brain;
        }
    }
    path.to_path_buf()
}

pub fn render(rows: &[Untitled]) -> String {
    if rows.is_empty() {
        return "every highlighted session has a headline\n".to_string();
    }
    let mut table = vec![vec![
        "tool".to_string(),
        "session".to_string(),
        "pages".to_string(),
        "source".to_string(),
        "title".to_string(),
    ]];
    for r in rows {
        table.push(vec![
            r.tool.clone(),
            r.session_id.chars().take(8).collect(),
            r.pages.len().to_string(),
            r.title_source.clone().unwrap_or_default(),
            r.title.as_deref().unwrap_or("-").chars().take(60).collect(),
        ]);
    }
    let mut s = crate::project::table(&table, 2);
    s.push_str(&format!(
        "{} highlighted sessions still titled by their opening line; `--json` lists transcripts\n",
        rows.len()
    ));
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    const NOW: i64 = 1_700_000_000_000;

    fn seed(s: &Store) {
        s.conn
            .execute_batch(&format!(
                "INSERT INTO sources (id, tool, path) VALUES
                    (1, 'claude-code', '/logs/a.jsonl'), (2, 'codex', '/logs/b.jsonl'),
                    (3, 'antigravity', '/ag/conversations/c.db');
                 INSERT INTO sessions (tool, session_id, source_id, title, display_title, title_source, active_ms)
                 VALUES ('claude-code', 'a', 1, 'first line', 'first line', 'first-line', 5000),
                        ('codex', 'b', 2, 'prompt', 'Named thread', 'codex-name', 4000),
                        ('antigravity', 'c', 3, '<t3>', '<t3>', 'first-line', 3000);
                 INSERT INTO requests (tool, session_id, source_id, request_key, ts_ms, output_tokens, cost_usd)
                 VALUES ('claude-code', 'a', 1, 'r1', {t}, 10, 1.0),
                        ('codex', 'b', 2, 'r2', {t}, 10, 2.0),
                        ('antigravity', 'c', 3, 'r3', {t}, 10, 0.5);",
                t = NOW - 1000
            ))
            .unwrap();
    }

    #[test]
    fn lists_weak_titles_on_every_page_they_appear() {
        let s = Store::open_in_memory().unwrap();
        seed(&s);
        let rows = untitled(&s.conn, NOW).unwrap();
        let mut ids: Vec<&str> = rows.iter().map(|r| r.session_id.as_str()).collect();
        ids.sort();
        // `b` has a Codex thread name, so only the two opening-line sessions remain.
        assert_eq!(ids, ["a", "c"]);
        let a = rows.iter().find(|r| r.session_id == "a").unwrap();
        let c = rows.iter().find(|r| r.session_id == "c").unwrap();
        assert_eq!(a.title_source.as_deref(), Some("first-line"));
        assert_eq!(a.path.as_deref(), Some("/logs/a.jsonl"));
        assert_eq!(a.transcript.as_deref(), Some("/logs/a.jsonl"));
        // Highlighted on the all-tools page and its own tool page, every window.
        assert!(a.pages.contains(&"4w".to_string()));
        assert!(a.pages.contains(&"1w/claude-code".to_string()));
        assert!(!a.pages.iter().any(|p| p.ends_with("/codex")));
        assert_eq!(a.pages.len(), 8);
        // No brain transcript on disk, so Antigravity falls back to the database.
        assert_eq!(c.transcript.as_deref(), Some("/ag/conversations/c.db"));
    }

    #[test]
    fn a_hand_set_title_removes_the_session() {
        let s = Store::open_in_memory().unwrap();
        seed(&s);
        crate::headlines::set_manual_title(&s.conn, "claude-code", "a", "Short summary", NOW)
            .unwrap();
        let rows = untitled(&s.conn, NOW).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].session_id, "c");
    }
}
