//! T3 enrichment reads legacy thread metadata and v2 cross-provider delegation
//! records. Native session ids resolve lineage across all supported harnesses.
//! Databases are opened read-only with `immutable=1`, ignoring WAL files.

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
        .flat_map(|d| {
            ["state.sqlite", "statev2.sqlite"].map(|name| home.join(d).join("userdata").join(name))
        })
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

/// Historical worktree ownership, including deleted worktrees and native
/// sessions that have no provider-session link. Conflicting roots are omitted.
pub fn project_aliases(path: &Path) -> Result<std::collections::BTreeMap<String, String>> {
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI;
    let conn = Connection::open_with_flags(sqlite_uri(path, "mode=ro&immutable=1"), flags)?;
    let mut stmt = conn.prepare(
        "SELECT t.worktree_path, MIN(p.workspace_root)
         FROM projection_threads t JOIN projection_projects p ON p.project_id = t.project_id
         WHERE t.worktree_path IS NOT NULL AND t.worktree_path != '' AND p.workspace_root != ''
         GROUP BY t.worktree_path HAVING COUNT(DISTINCT p.workspace_root) = 1",
    )?;
    let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
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
        // A deleted T3 worktree still belongs to its recorded parent workspace.
        assert_eq!(
            row(&store.conn, "antigravity", "ag-1"),
            (
                Some("thread-ag".into()),
                Some("Draft a plan".into()),
                Some(crate::project::resolve(ws))
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

fn tool_name(provider: &str) -> Option<&'static str> {
    match provider {
        "codex" => Some("codex"),
        "claudeAgent" | "claude-code" => Some("claude-code"),
        "antigravity" => Some("antigravity"),
        _ => None,
    }
}

type SessionKey = (String, String);

#[derive(Debug)]
struct Delegation {
    id: String,
    parent: SessionKey,
    child: Option<SessionKey>,
    child_tool: String,
    started: Option<i64>,
}

fn has_table(conn: &Connection, table: &str) -> bool {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name = ?1)",
        [table],
        |r| r.get(0),
    )
    .unwrap_or(false)
}

fn load_delegations(path: &Path) -> Result<Vec<Delegation>> {
    let conn = Connection::open_with_flags(
        sqlite_uri(path, "mode=ro&immutable=1"),
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    if !has_table(&conn, "orchestration_v2_projection_subagents") {
        return Ok(vec![]);
    }
    let mut threads: HashMap<String, SessionKey> = HashMap::new();
    let mut provider_threads: HashMap<String, SessionKey> = HashMap::new();
    if has_table(&conn, "provider_session_runtime") {
        let mut stmt = conn.prepare("SELECT thread_id, provider_name, resume_cursor_json FROM provider_session_runtime ORDER BY thread_id")?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let provider: String = r.get(1)?;
            let cursor: Option<String> = r.get(2)?;
            if let (Some(tool), Some(id)) = (
                tool_name(&provider),
                cursor.as_deref().and_then(|v| {
                    let value: serde_json::Value = serde_json::from_str(v).ok()?;
                    let field = if provider == "claudeAgent" {
                        "resume"
                    } else if provider == "codex" {
                        "threadId"
                    } else {
                        "sessionId"
                    };
                    value
                        .get(field)?
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .map(str::to_owned)
                }),
            ) {
                threads.insert(r.get(0)?, (tool.into(), id));
            }
        }
    }
    if has_table(&conn, "orchestration_v2_projection_provider_threads") {
        let mut stmt = conn.prepare("SELECT thread_id, provider, payload_json, provider_thread_id FROM orchestration_v2_projection_provider_threads ORDER BY updated_at, provider_thread_id")?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let provider: String = r.get(1)?;
            let payload: String = r.get(2)?;
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&payload) else {
                continue;
            };
            if let (Some(tool), Some(id)) = (
                tool_name(&provider),
                v.pointer("/nativeThreadRef/nativeId")
                    .and_then(|v| v.as_str())
                    .filter(|s| !s.is_empty()),
            ) {
                let key = (tool.to_owned(), id.to_owned());
                provider_threads.insert(r.get(3)?, key.clone());
                threads.insert(r.get(0)?, key);
            }
        }
    }
    let mut out = Vec::new();
    let has_provider_thread = conn
        .prepare("SELECT provider_thread_id FROM orchestration_v2_projection_subagents LIMIT 0")
        .is_ok();
    let sql = format!(
        "SELECT thread_id, child_thread_id, provider, started_at, subagent_id, {} FROM orchestration_v2_projection_subagents ORDER BY started_at, subagent_id",
        if has_provider_thread {
            "provider_thread_id"
        } else {
            "NULL"
        }
    );
    let mut stmt = conn.prepare(&sql)?;
    let mut rows = stmt.query([])?;
    while let Some(r) = rows.next()? {
        let parent: String = r.get(0)?;
        let child: Option<String> = r.get(1)?;
        let provider: String = r.get(2)?;
        let started: Option<String> = r.get(3)?;
        let (Some(parent), Some(child_tool)) = (threads.get(&parent), tool_name(&provider)) else {
            continue;
        };
        let provider_thread: Option<String> = r.get(5)?;
        out.push(Delegation {
            id: r.get(4)?,
            parent: parent.clone(),
            child: child
                .and_then(|c| threads.get(&c).cloned())
                .or_else(|| provider_thread.and_then(|id| provider_threads.get(&id).cloned())),
            child_tool: child_tool.into(),
            started: started
                .and_then(|v| chrono::DateTime::parse_from_rfc3339(&v).ok())
                .map(|v| v.timestamp_millis()),
        });
    }
    Ok(out)
}

/// Import only unambiguous, unclaimed children. Harness parents always win.
/// Native ids are authoritative; timestamps are a fallback only when the native
/// child cannot be found in the usage store. No title participates in matching.
pub fn enrich_lineage(tx: &Transaction, paths: &[PathBuf]) -> Result<()> {
    let mut delegations = Vec::new();
    for path in paths.iter().filter(|p| p.is_file()) {
        match load_delegations(path) {
            Ok(rows) => delegations.extend(rows),
            Err(_) => continue, // Optional enrichment must not prevent ingest.
        }
    }
    let exists = |key: &SessionKey| -> Result<bool> {
        Ok(tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM sessions WHERE tool = ?1 AND session_id = ?2)",
            params![key.0, key.1],
            |r| r.get(0),
        )?)
    };
    // Resolve candidates before writing, so two delegations cannot win the
    // same timing candidate merely because one happened to be processed first.
    let mut pending = Vec::new();
    let mut claims: HashMap<SessionKey, std::collections::HashSet<String>> = HashMap::new();
    for d in &delegations {
        if let Some(child) = &d.child {
            claims
                .entry(child.clone())
                .or_default()
                .insert(d.id.clone());
        }
    }
    for d in delegations {
        let already_imported: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM sessions WHERE lineage_id = ?1)",
            [&d.id],
            |r| r.get(0),
        )?;
        if already_imported {
            continue;
        }
        if !exists(&d.parent)? {
            continue;
        }
        let exact = d.child.as_ref().filter(|c| exists(c).unwrap_or(false));
        let (child, source) = if let Some(child) = exact {
            (child.clone(), "t3")
        } else {
            let Some(start) = d.started else { continue };
            let candidates = tx.prepare(
                "SELECT s.tool, s.session_id FROM sessions s JOIN sessions p ON p.tool = ?1 AND p.session_id = ?2
                 WHERE s.tool = ?3 AND s.parent_session_id IS NULL
                   AND (SELECT MIN(start_ms) FROM session_events e WHERE e.tool = s.tool AND e.session_id = s.session_id) BETWEEN ?4 AND ?4 + 60000
                   AND (COALESCE(s.project, s.cwd) IS NULL OR COALESCE(p.project, p.cwd) IS NULL
                        OR COALESCE(s.project, s.cwd) = COALESCE(p.project, p.cwd))")?
                .query_map(params![d.parent.0, d.parent.1, d.child_tool, start], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            if candidates.len() != 1 {
                continue;
            }
            (candidates[0].clone(), "timing")
        };
        if child != d.parent {
            pending.push((d.parent, child, source, d.id));
        }
    }
    for (_, child, _, id) in &pending {
        claims.entry(child.clone()).or_default().insert(id.clone());
    }
    // Exact evidence takes priority over timing evidence.
    pending.sort_by_key(|(_, _, source, _)| *source == "timing");
    for (parent, child, source, id) in pending {
        if source == "timing" && claims[&child].len() != 1 {
            continue;
        }
        // Reject cycles before writing. UNION also bounds traversal of existing malformed trees.
        let cycle: bool = tx.query_row(
            "WITH RECURSIVE ancestors(tool, sid) AS (
                SELECT ?1, ?2 UNION SELECT COALESCE(s.parent_tool, s.tool), s.parent_session_id FROM sessions s
                JOIN ancestors a ON s.tool = a.tool AND s.session_id = a.sid WHERE s.parent_session_id IS NOT NULL
             ) SELECT EXISTS(SELECT 1 FROM ancestors WHERE tool = ?3 AND sid = ?4)",
            params![parent.0, parent.1, child.0, child.1], |r| r.get(0))?;
        if cycle {
            continue;
        }
        let changed = tx.execute("UPDATE sessions SET parent_tool = ?3, parent_session_id = ?4, lineage_source = ?5, lineage_id = ?6, is_subagent = 1
            WHERE tool = ?1 AND session_id = ?2 AND parent_session_id IS NULL", params![child.0, child.1, parent.0, parent.1, source, id])?;
        if changed > 0 {
            tx.execute(
                "UPDATE requests SET is_subagent = 1 WHERE tool = ?1 AND session_id = ?2",
                params![child.0, child.1],
            )?;
            crate::store::refresh_ancestors(tx, &child.0, &child.1)?;
        }
    }
    Ok(())
}
