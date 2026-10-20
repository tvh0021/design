//! T3 Code enrichment. T3's `state.sqlite` maps its threads to the Codex and
//! Antigravity sessions it drives; this attaches the T3 thread id and title to
//! those sessions and supplies a working directory where the harness log has
//! none. The database is opened `immutable=1` (read-only, WAL ignored) and is
//! never written. Claude Code sessions get no T3 link.

use crate::antigravity::sqlite_uri;
use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags, Transaction, params};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// What T3 knows about one harness session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Link {
    pub thread_id: String,
    pub title: Option<String>,
    /// The thread's worktree when it has one, else its project's workspace root.
    pub workspace: Option<String>,
}

/// Keyed by (tool, harness session id).
pub type Links = HashMap<(String, String), Link>;

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct T3Report {
    pub databases_read: u64,
    pub databases_unreadable: u64,
    /// Sessions that got a T3 thread id this run.
    pub sessions_linked: u64,
}

/// T3 state databases that exist on this machine, in priority order: the
/// standard `~/.t3` install, then the `~/.t3-personal` profile.
pub fn default_paths() -> Vec<PathBuf> {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else {
        return Vec::new();
    };
    [".t3", ".t3-personal"]
        .iter()
        .map(|d| home.join(d).join("userdata/state.sqlite"))
        .filter(|p| p.is_file())
        .collect()
}

/// Read every Codex and Antigravity session link from one state database.
pub fn load(path: &Path) -> Result<Links> {
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY
        | OpenFlags::SQLITE_OPEN_URI
        | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let conn = Connection::open_with_flags(sqlite_uri(path, "mode=ro&immutable=1"), flags)
        .with_context(|| format!("opening {}", path.display()))?;
    let mut stmt = conn.prepare(
        r#"SELECT r.thread_id, r.provider_name, r.resume_cursor_json,
                  t.title, t.worktree_path, p.workspace_root
           FROM provider_session_runtime r
           LEFT JOIN projection_threads t ON t.thread_id = r.thread_id
           LEFT JOIN projection_projects p ON p.project_id = t.project_id
           WHERE r.provider_name IN ('codex', 'antigravity')
           ORDER BY r.thread_id"#,
    )?;
    let mut rows = stmt.query([])?;
    let mut out = Links::new();
    while let Some(r) = rows.next()? {
        let thread_id: String = r.get(0)?;
        let provider: String = r.get(1)?;
        let cursor: Option<String> = r.get(2)?;
        let Some(session_id) = cursor.as_deref().and_then(|c| session_id(&provider, c)) else {
            continue;
        };
        let nonempty = |s: Option<String>| s.filter(|s| !s.is_empty());
        let title = nonempty(r.get(3)?);
        let workspace = nonempty(r.get(4)?).or(nonempty(r.get(5)?));
        out.entry((provider, session_id)).or_insert(Link {
            thread_id,
            title,
            workspace,
        });
    }
    Ok(out)
}

/// The harness session id in a `resume_cursor_json`: Codex rows carry
/// `{"threadId": ...}`, Antigravity rows `{"schemaVersion": 1, "sessionId": ...}`.
fn session_id(provider: &str, cursor: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(cursor).ok()?;
    let field = match provider {
        "codex" => "threadId",
        "antigravity" => "sessionId",
        _ => return None,
    };
    v.get(field)?
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// Add `more` to `links`; a session already linked by an earlier database keeps
/// that link.
pub fn merge(links: &mut Links, more: Links) {
    for (k, v) in more {
        links.entry(k).or_insert(v);
    }
}

/// Write thread ids and titles. A session that has no link yet gets one; a
/// session already linked to the same thread gets the current title. Returns
/// the number of sessions newly linked.
pub fn apply(tx: &Transaction, links: &Links) -> Result<u64> {
    let mut link = tx.prepare(
        "UPDATE sessions SET t3_thread_id = ?3, t3_title = ?4
         WHERE tool = ?1 AND session_id = ?2 AND t3_thread_id IS NULL",
    )?;
    let mut retitle = tx.prepare(
        "UPDATE sessions SET t3_title = ?4
         WHERE tool = ?1 AND session_id = ?2 AND t3_thread_id = ?3 AND t3_title IS NOT ?4",
    )?;
    let mut linked = 0;
    for ((tool, session_id), l) in links {
        let p = params![tool, session_id, l.thread_id, l.title];
        linked += link.execute(p)? as u64;
        retitle.execute(p)?;
    }
    Ok(linked)
}

#[cfg(test)]
pub(crate) mod fixture {
    use rusqlite::{Connection, params};
    use std::path::Path;

    /// (thread id, provider, resume cursor, title, worktree, project id).
    pub type Thread<'a> = (&'a str, &'a str, &'a str, &'a str, Option<&'a str>, &'a str);

    /// A minimal T3 state database with the three tables enrichment reads.
    pub fn build(path: &Path, projects: &[(&str, &str, &str)], threads: &[Thread]) {
        let c = Connection::open(path).unwrap();
        c.execute_batch(
            r#"CREATE TABLE provider_session_runtime (
                   thread_id TEXT PRIMARY KEY, provider_name TEXT NOT NULL,
                   adapter_key TEXT NOT NULL, status TEXT NOT NULL,
                   last_seen_at TEXT NOT NULL, resume_cursor_json TEXT);
               CREATE TABLE projection_threads (
                   thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                   title TEXT NOT NULL, worktree_path TEXT,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
               CREATE TABLE projection_projects (
                   project_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                   workspace_root TEXT NOT NULL, scripts_json TEXT NOT NULL,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"#,
        )
        .unwrap();
        for (id, title, root) in projects {
            c.execute(
                "INSERT INTO projection_projects VALUES (?1, ?2, ?3, '[]', 't', 't')",
                params![id, title, root],
            )
            .unwrap();
        }
        for (tid, provider, cursor, title, worktree, project) in threads {
            c.execute(
                "INSERT INTO provider_session_runtime VALUES (?1, ?2, ?2, 'stopped', 't', ?3)",
                params![tid, provider, cursor],
            )
            .unwrap();
            c.execute(
                "INSERT INTO projection_threads VALUES (?1, ?2, ?3, ?4, 't', 't')",
                params![tid, project, title, worktree],
            )
            .unwrap();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    fn session(conn: &Connection, tool: &str, sid: &str, cwd: Option<&str>) {
        conn.execute(
            "INSERT INTO sessions (tool, session_id, cwd) VALUES (?1, ?2, ?3)",
            params![tool, sid, cwd],
        )
        .unwrap();
    }

    #[test]
    fn links_codex_and_antigravity_sessions() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("state.sqlite");
        let ws = tmp.path().join("workspace");
        std::fs::create_dir_all(&ws).unwrap();
        let ws = ws.to_str().unwrap();
        fixture::build(
            &db,
            &[("proj-1", "Demo project", ws)],
            &[
                (
                    "thread-codex",
                    "codex",
                    r#"{"threadId":"cx-1"}"#,
                    "Fix the parser",
                    None,
                    "proj-1",
                ),
                (
                    "thread-ag",
                    "antigravity",
                    r#"{"schemaVersion":1,"sessionId":"ag-1"}"#,
                    "Draft a plan",
                    Some("/nowhere/worktree"),
                    "proj-1",
                ),
                (
                    "thread-claude",
                    "claudeAgent",
                    r#"{"threadId":"cc-1"}"#,
                    "Ignored",
                    None,
                    "proj-1",
                ),
                ("thread-bad", "codex", "not json", "Ignored", None, "proj-1"),
            ],
        );
        let links = load(&db).unwrap();
        assert_eq!(links.len(), 2);
        let ag = &links[&("antigravity".to_string(), "ag-1".to_string())];
        assert_eq!(ag.thread_id, "thread-ag");
        assert_eq!(ag.workspace.as_deref(), Some("/nowhere/worktree"));

        let mut store = Store::open_in_memory().unwrap();
        session(&store.conn, "codex", "cx-1", Some("/recorded/cwd"));
        session(&store.conn, "antigravity", "ag-1", None);
        session(&store.conn, "claude-code", "cc-1", Some("/recorded/cwd"));
        session(&store.conn, "codex", "cx-unknown", None);
        let r = crate::project::enrich(&mut store.conn, std::slice::from_ref(&db)).unwrap();
        assert_eq!(r.t3.databases_read, 1);
        assert_eq!(r.t3.sessions_linked, 2);
        assert_eq!(r.projects_set, 3);
        assert_eq!(r.projects_from_t3, 1);
        assert_eq!(r.without_cwd, 1);

        type Row = (Option<String>, Option<String>, Option<String>);
        let row = |conn: &Connection, tool: &str, sid: &str| -> Row {
            conn.query_row(
                "SELECT t3_thread_id, t3_title, project FROM sessions
                     WHERE tool = ?1 AND session_id = ?2",
                params![tool, sid],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap()
        };
        assert_eq!(
            row(&store.conn, "codex", "cx-1"),
            (
                Some("thread-codex".into()),
                Some("Fix the parser".into()),
                Some("/recorded/cwd".into())
            )
        );
        // No recorded cwd: the T3 worktree path stands in (missing on disk, kept as is).
        assert_eq!(
            row(&store.conn, "antigravity", "ag-1"),
            (
                Some("thread-ag".into()),
                Some("Draft a plan".into()),
                Some("/nowhere/worktree".into())
            )
        );
        assert_eq!(row(&store.conn, "claude-code", "cc-1").0, None);
        assert_eq!(row(&store.conn, "codex", "cx-unknown"), (None, None, None));

        // Re-running links nothing new and keeps the data.
        let r = crate::project::enrich(&mut store.conn, &[db]).unwrap();
        assert_eq!(r.t3.sessions_linked, 0);
        assert_eq!(
            row(&store.conn, "codex", "cx-1").0.as_deref(),
            Some("thread-codex")
        );
    }

    #[test]
    fn unreadable_database_is_skipped() {
        let tmp = tempfile::tempdir().unwrap();
        let bad = tmp.path().join("state.sqlite");
        std::fs::write(&bad, b"this is not a sqlite database at all, just text").unwrap();
        assert!(load(&bad).is_err());
        let mut store = Store::open_in_memory().unwrap();
        session(&store.conn, "codex", "cx-1", Some("/recorded/cwd"));
        let r = crate::project::enrich(&mut store.conn, &[bad]).unwrap();
        assert_eq!(r.t3.databases_unreadable, 1);
        assert_eq!(r.t3.databases_read, 0);
        assert_eq!(r.projects_set, 1);
    }

    #[test]
    fn earlier_database_wins() {
        let mut a = Links::new();
        let k = ("codex".to_string(), "s".to_string());
        a.insert(
            k.clone(),
            Link {
                thread_id: "first".into(),
                title: None,
                workspace: None,
            },
        );
        let mut b = Links::new();
        b.insert(
            k.clone(),
            Link {
                thread_id: "second".into(),
                title: None,
                workspace: None,
            },
        );
        merge(&mut a, b);
        assert_eq!(a[&k].thread_id, "first");
    }
}
