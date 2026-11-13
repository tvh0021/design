//! T3 enrichment reads v1 and v2 thread metadata and v2 cross-provider delegation
//! records. Native session ids resolve lineage across all supported harnesses.
//! Databases are opened read-only with `immutable=1`, ignoring WAL files.

use crate::antigravity::sqlite_uri;
use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags, Transaction, params};
use std::collections::{BTreeMap, BTreeSet, HashMap};
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
    /// Sessions that got a new or changed T3 thread id this run.
    pub sessions_linked: u64,
}

/// T3 state databases that exist on this machine, in priority order: the
/// standard `~/.t3` install, then the `~/.t3-personal` profile. Within a
/// profile the live v2 database comes before a frozen v1 snapshot.
pub fn default_paths() -> Vec<PathBuf> {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else {
        return Vec::new();
    };
    [".t3", ".t3-personal"]
        .iter()
        .flat_map(|d| {
            ["statev2.sqlite", "state.sqlite"].map(|name| home.join(d).join("userdata").join(name))
        })
        .filter(|p| p.is_file())
        .collect()
}

/// Open a state database read-only and fail early when it is not SQLite, so
/// the optional-table checks below cannot mistake a corrupt file for an
/// empty one.
fn open(path: &Path) -> Result<Connection> {
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY
        | OpenFlags::SQLITE_OPEN_URI
        | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let conn = Connection::open_with_flags(sqlite_uri(path, "mode=ro&immutable=1"), flags)
        .with_context(|| format!("opening {}", path.display()))?;
    conn.prepare("SELECT 1 FROM sqlite_master LIMIT 1")?
        .query([])?
        .next()?;
    Ok(conn)
}

fn nonempty(s: Option<String>) -> Option<String> {
    s.filter(|s| !s.is_empty())
}

/// T3 copies the raw opening lines of an imported external transcript into
/// the thread title instead of generating a headline, so those titles are
/// dropped.
fn imported(thread_id: &str, history_origin: Option<&str>) -> bool {
    thread_id.starts_with("import:") || history_origin == Some("v1_import")
}

/// Read every session link from one state database: v2 provider threads
/// first (newest first), then legacy `provider_session_runtime` rows for
/// sessions v2 does not name.
pub fn load(path: &Path) -> Result<Links> {
    let conn = open(path)?;
    let projects = has_table(&conn, "projection_projects");
    let mut out = Links::new();
    if has_table(&conn, "orchestration_v2_projection_provider_threads")
        && has_table(&conn, "orchestration_v2_projection_threads")
    {
        let (root, join) = if projects {
            (
                "p.workspace_root",
                "LEFT JOIN projection_projects p ON p.project_id = t.project_id",
            )
        } else {
            ("NULL", "")
        };
        let mut stmt = conn.prepare(&format!(
            "SELECT pt.thread_id, pt.provider, pt.payload_json, t.title, t.payload_json, {root}
             FROM orchestration_v2_projection_provider_threads pt
             LEFT JOIN orchestration_v2_projection_threads t ON t.thread_id = pt.thread_id
             {join}
             ORDER BY pt.updated_at DESC, pt.provider_thread_id DESC"
        ))?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let Some(thread_id) = nonempty(r.get(0)?) else {
                continue;
            };
            let provider: String = r.get(1)?;
            let payload: Option<String> = r.get(2)?;
            let native = payload
                .and_then(|p| serde_json::from_str::<serde_json::Value>(&p).ok())
                .and_then(|v| {
                    v.pointer("/nativeThreadRef/nativeId")?
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .map(str::to_owned)
                });
            let (Some(tool), Some(native)) = (tool_name(&provider), native) else {
                continue;
            };
            let thread: Option<serde_json::Value> = r
                .get::<_, Option<String>>(4)?
                .and_then(|p| serde_json::from_str(&p).ok());
            let field = |name: &str| {
                thread
                    .as_ref()
                    .and_then(|v| v.pointer(name)?.as_str())
                    .filter(|s| !s.is_empty())
                    .map(str::to_owned)
            };
            let title = nonempty(r.get(3)?)
                .filter(|_| !imported(&thread_id, field("/historyOrigin").as_deref()));
            let workspace = field("/worktreePath").or(nonempty(r.get(5)?));
            out.entry((tool.to_owned(), native)).or_insert(Link {
                thread_id,
                title,
                workspace,
            });
        }
    }
    if has_table(&conn, "provider_session_runtime") {
        let threads = has_table(&conn, "projection_threads");
        let (cols, joins) = match (threads, projects) {
            (true, true) => (
                "t.title, t.worktree_path, p.workspace_root",
                "LEFT JOIN projection_threads t ON t.thread_id = r.thread_id
                 LEFT JOIN projection_projects p ON p.project_id = t.project_id",
            ),
            (true, false) => (
                "t.title, t.worktree_path, NULL",
                "LEFT JOIN projection_threads t ON t.thread_id = r.thread_id",
            ),
            _ => ("NULL, NULL, NULL", ""),
        };
        let mut stmt = conn.prepare(&format!(
            "SELECT r.thread_id, r.provider_name, r.resume_cursor_json, {cols}
             FROM provider_session_runtime r {joins}
             ORDER BY r.thread_id"
        ))?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let thread_id: String = r.get(0)?;
            let provider: String = r.get(1)?;
            let cursor: Option<String> = r.get(2)?;
            let Some(key) = cursor.as_deref().and_then(|c| cursor_session(&provider, c)) else {
                continue;
            };
            let title = nonempty(r.get(3)?).filter(|_| !imported(&thread_id, None));
            let workspace = nonempty(r.get(4)?).or(nonempty(r.get(5)?));
            out.entry(key).or_insert(Link {
                thread_id,
                title,
                workspace,
            });
        }
    }
    Ok(out)
}

/// Historical worktree ownership from v1 and v2 threads, including deleted
/// worktrees and native sessions that have no provider-session link. A
/// worktree recorded under more than one workspace root is omitted.
pub fn project_aliases(path: &Path) -> Result<BTreeMap<String, String>> {
    let conn = open(path)?;
    let mut roots: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    if !has_table(&conn, "projection_projects") {
        return Ok(BTreeMap::new());
    }
    if has_table(&conn, "projection_threads") {
        let mut stmt = conn.prepare(
            "SELECT t.worktree_path, p.workspace_root
             FROM projection_threads t JOIN projection_projects p ON p.project_id = t.project_id
             WHERE t.worktree_path IS NOT NULL AND t.worktree_path != '' AND p.workspace_root != ''",
        )?;
        for row in stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))? {
            let (worktree, root) = row?;
            insert_alias(&mut roots, &worktree, &root);
        }
    }
    if has_table(&conn, "orchestration_v2_projection_threads") {
        let mut stmt = conn.prepare(
            "SELECT t.payload_json, p.workspace_root
             FROM orchestration_v2_projection_threads t
             JOIN projection_projects p ON p.project_id = t.project_id
             WHERE p.workspace_root != ''",
        )?;
        for row in stmt.query_map([], |r| {
            Ok((r.get::<_, Option<String>>(0)?, r.get::<_, String>(1)?))
        })? {
            let (payload, root) = row?;
            let worktree = payload
                .and_then(|p| serde_json::from_str::<serde_json::Value>(&p).ok())
                .and_then(|v| v.get("worktreePath")?.as_str().map(str::to_owned));
            if let Some(worktree) = worktree {
                insert_alias(&mut roots, &worktree, &root);
            }
        }
    }
    Ok(roots
        .into_iter()
        .filter(|(_, r)| r.len() == 1)
        .filter_map(|(w, r)| Some((w, r.into_iter().next()?)))
        .collect())
}

/// Record one worktree-to-root pair when both are absolute, non-root paths,
/// the rule `config.rs` applies to hand-written aliases, so relative or `/`
/// paths in T3 metadata cannot capture project resolution.
fn insert_alias(roots: &mut BTreeMap<String, BTreeSet<String>>, worktree: &str, root: &str) {
    let ok = |p: &str| Path::new(p).is_absolute() && p != "/";
    let (worktree, root) = (worktree.trim(), root.trim());
    if ok(worktree) && ok(root) {
        roots
            .entry(worktree.to_owned())
            .or_default()
            .insert(root.to_owned());
    }
}

/// The harness session named by a legacy `resume_cursor_json`: Claude rows
/// carry `{"resume": ...}`, Codex rows `{"threadId": ...}`, Antigravity rows
/// `{"schemaVersion": 1, "sessionId": ...}`.
fn cursor_session(provider: &str, cursor: &str) -> Option<SessionKey> {
    let field = match provider {
        "claudeAgent" | "claude-code" => "resume",
        "codex" => "threadId",
        "antigravity" => "sessionId",
        _ => return None,
    };
    let v: serde_json::Value = serde_json::from_str(cursor).ok()?;
    let id = v.get(field)?.as_str().filter(|s| !s.is_empty())?;
    Some((tool_name(provider)?.to_owned(), id.to_owned()))
}

/// Add `more` to `links`; a session already linked by an earlier database keeps
/// that link.
pub fn merge(links: &mut Links, more: Links) {
    for (k, v) in more {
        links.entry(k).or_insert(v);
    }
}

/// Write thread ids and titles. A session with no link, or linked to another
/// thread (such as a frozen v1 or `import:` thread now superseded by v2), takes
/// the winning thread and its title; a session already linked to the same
/// thread gets the current title. Returns the number of sessions newly linked
/// or relinked.
pub fn apply(tx: &Transaction, links: &Links) -> Result<u64> {
    let mut link = tx.prepare(
        "UPDATE sessions SET t3_thread_id = ?3, t3_title = ?4
         WHERE tool = ?1 AND session_id = ?2 AND t3_thread_id IS NOT ?3",
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
    fn links_v2_threads_drops_imported_titles_and_folds_v2_worktrees() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("statev2.sqlite");
        fixture::build(
            &db,
            &[("proj-a", "A", "/work/a"), ("proj-b", "B", "/work/b")],
            &[
                (
                    "v1-claude",
                    "claudeAgent",
                    r#"{"resume":"cc-1"}"#,
                    "V1 claude",
                    None,
                    "proj-a",
                ),
                (
                    "v1-shadowed",
                    "claudeAgent",
                    r#"{"resume":"cc-2"}"#,
                    "Old",
                    None,
                    "proj-a",
                ),
                (
                    "import:v1",
                    "codex",
                    r#"{"threadId":"cx-imp"}"#,
                    "<raw opening>",
                    None,
                    "proj-a",
                ),
                (
                    "v1-wt",
                    "codex",
                    r#"{"threadId":"cx-wt"}"#,
                    "Wt",
                    Some("/wt/conflict"),
                    "proj-a",
                ),
            ],
        );
        let c = Connection::open(&db).unwrap();
        c.execute_batch(
            "CREATE TABLE orchestration_v2_projection_provider_threads (
                 provider_thread_id TEXT PRIMARY KEY, thread_id TEXT, provider TEXT NOT NULL,
                 updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
             CREATE TABLE orchestration_v2_projection_threads (
                 thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                 payload_json TEXT NOT NULL);",
        )
        .unwrap();
        let native =
            |id: &str| serde_json::json!({"nativeThreadRef": {"nativeId": id}}).to_string();
        for (pt, thread, provider, updated, payload) in [
            ("p1", Some("t-claude"), "claudeAgent", "2", native("cc-2")),
            ("p2", Some("t-codex"), "codex", "2", native("cx-1")),
            ("p3", Some("t-codex-old"), "codex", "1", native("cx-1")),
            ("p4", Some("t-ag"), "antigravity", "2", native("ag-1")),
            ("p5", Some("t-imported"), "codex", "2", native("cx-v1imp")),
            ("p6", None, "codex", "2", native("cx-orphan")),
            ("p7", Some("t-codex"), "codex", "2", "{}".into()),
        ] {
            c.execute(
                "INSERT INTO orchestration_v2_projection_provider_threads VALUES (?1, ?2, ?3, ?4, ?5)",
                params![pt, thread, provider, updated, payload],
            )
            .unwrap();
        }
        for (tid, project, title, payload) in [
            (
                "t-claude",
                "proj-b",
                "V2 claude",
                r#"{"worktreePath":"/wt/deleted"}"#,
            ),
            ("t-codex", "proj-a", "V2 codex", "{}"),
            ("t-codex-old", "proj-a", "Older codex", "{}"),
            ("t-ag", "proj-b", "", r#"{"worktreePath":""}"#),
            (
                "t-imported",
                "proj-a",
                "<raw opening>",
                r#"{"historyOrigin":"v1_import"}"#,
            ),
            (
                "t-conflict",
                "proj-b",
                "X",
                r#"{"worktreePath":"/wt/conflict"}"#,
            ),
            ("t-relative", "proj-a", "R", r#"{"worktreePath":"rel/wt"}"#),
            ("t-root", "proj-b", "R", r#"{"worktreePath":"/"}"#),
        ] {
            c.execute(
                "INSERT INTO orchestration_v2_projection_threads VALUES (?1, ?2, ?3, ?4)",
                params![tid, project, title, payload],
            )
            .unwrap();
        }
        drop(c);

        let links = load(&db).unwrap();
        let get = |tool: &str, sid: &str| {
            let l = &links[&(tool.to_string(), sid.to_string())];
            (
                l.thread_id.as_str(),
                l.title.as_deref(),
                l.workspace.as_deref(),
            )
        };
        assert_eq!(links.len(), 7);
        assert_eq!(
            get("claude-code", "cc-1"),
            ("v1-claude", Some("V1 claude"), Some("/work/a"))
        );
        // v2 wins over v1 within one database, and the newest provider thread wins.
        assert_eq!(
            get("claude-code", "cc-2"),
            ("t-claude", Some("V2 claude"), Some("/wt/deleted"))
        );
        assert_eq!(
            get("codex", "cx-1"),
            ("t-codex", Some("V2 codex"), Some("/work/a"))
        );
        assert_eq!(get("antigravity", "ag-1"), ("t-ag", None, Some("/work/b")));
        assert_eq!(
            get("codex", "cx-v1imp"),
            ("t-imported", None, Some("/work/a"))
        );
        assert_eq!(get("codex", "cx-imp"), ("import:v1", None, Some("/work/a")));

        let aliases = project_aliases(&db).unwrap();
        assert_eq!(
            aliases.get("/wt/deleted").map(String::as_str),
            Some("/work/b")
        );
        assert!(!aliases.contains_key("/wt/conflict"));
        assert!(!aliases.contains_key("rel/wt") && !aliases.contains_key("/"));

        // A stored imported title is cleared on the next apply.
        let mut store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch(
                "INSERT INTO sessions (tool, session_id, t3_thread_id, t3_title)
                 VALUES ('codex', 'cx-v1imp', 't-imported', '<raw opening>'),
                        ('codex', 'cx-imp', 'import:v1', '<raw opening>');",
            )
            .unwrap();
        let tx = store.conn.transaction().unwrap();
        apply(&tx, &links).unwrap();
        tx.commit().unwrap();
        let stored: i64 = store
            .conn
            .query_row(
                "SELECT COUNT(*) FROM sessions WHERE t3_title IS NOT NULL",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(stored, 0);

        // A session linked by an earlier scan to a frozen v1 thread, with a
        // poisoned title, moves to the winning v2 thread. An orphan provider
        // thread (null thread_id) still resolves a delegation's child through
        // its provider_thread_id.
        let c = Connection::open(&db).unwrap();
        c.execute_batch(
            "CREATE TABLE orchestration_v2_projection_subagents (
                 subagent_id TEXT, thread_id TEXT, child_thread_id TEXT, provider TEXT,
                 started_at TEXT, provider_thread_id TEXT);
             INSERT INTO orchestration_v2_projection_subagents
                 VALUES ('d-orphan', 't-codex', NULL, 'codex', '2026-01-01T00:01:00Z', 'p6'),
                        ('d-empty', 't-codex', '', 'codex', NULL, '');",
        )
        .unwrap();
        drop(c);
        let mut store = Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch(
                "INSERT INTO sessions (tool, session_id, t3_thread_id, t3_title)
                 VALUES ('codex', 'cx-1', 'v1-old', '<recommended_plugins>');
                 INSERT INTO sessions (tool, session_id) VALUES ('codex', 'cx-orphan');",
            )
            .unwrap();
        let r = crate::project::enrich(&mut store.conn, std::slice::from_ref(&db)).unwrap();
        assert_eq!(r.t3.sessions_linked, 1);
        let got: (String, String, String, String, String) = store
            .conn
            .query_row(
                "SELECT p.t3_thread_id, p.t3_title, c.parent_session_id, c.lineage_source, c.lineage_id
                 FROM sessions p, sessions c
                 WHERE p.session_id = 'cx-1' AND c.session_id = 'cx-orphan'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .unwrap();
        assert_eq!(
            got,
            (
                "t-codex".into(),
                "V2 codex".into(),
                "cx-1".into(),
                "t3".into(),
                "d-orphan".into()
            )
        );
        let r = crate::project::enrich(&mut store.conn, &[db]).unwrap();
        assert_eq!(r.t3.sessions_linked, 0);
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
    let conn = open(path)?;
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
            if let Some(key) = cursor.as_deref().and_then(|c| cursor_session(&provider, c)) {
                threads.insert(r.get(0)?, key);
            }
        }
    }
    if has_table(&conn, "orchestration_v2_projection_provider_threads") {
        let mut stmt = conn.prepare("SELECT thread_id, provider, payload_json, provider_thread_id FROM orchestration_v2_projection_provider_threads ORDER BY updated_at, provider_thread_id")?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            // Orphan and subagent provider threads have no T3 thread id.
            let thread_id = nonempty(r.get(0)?);
            let provider: String = r.get(1)?;
            let Some(payload) = nonempty(r.get(2)?) else {
                continue;
            };
            let provider_thread_id = nonempty(r.get(3)?);
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
                if let Some(pt) = provider_thread_id {
                    provider_threads.insert(pt, key.clone());
                }
                if let Some(t) = thread_id {
                    threads.insert(t, key);
                }
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
        let Some(parent) = nonempty(r.get(0)?) else {
            continue;
        };
        let child = nonempty(r.get(1)?);
        let provider: String = r.get(2)?;
        let started: Option<String> = r.get(3)?;
        let (Some(parent), Some(child_tool)) = (threads.get(&parent), tool_name(&provider)) else {
            continue;
        };
        let provider_thread = nonempty(r.get(5)?);
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
