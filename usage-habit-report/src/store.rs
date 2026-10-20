//! SQLite store: schema, migrations, and the write helpers the ingesters share.
//!
//! The schema is versioned with `PRAGMA user_version`. Later tickets append to
//! `MIGRATIONS`; never edit a migration that has shipped.

use anyhow::{Context, Result};
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use std::path::Path;

/// Each entry moves the schema from version `i` to `i + 1`.
const MIGRATIONS: &[&str] = &[
    // v1: sources, sessions, requests.
    r#"
    CREATE TABLE sources (
        id          INTEGER PRIMARY KEY,
        tool        TEXT    NOT NULL,          -- 'claude-code', later 'codex', 'antigravity'
        path        TEXT    NOT NULL UNIQUE,   -- absolute path of the raw log file
        offset      INTEGER NOT NULL DEFAULT 0,-- bytes consumed (always ends on a line boundary)
        size        INTEGER NOT NULL DEFAULT 0,-- file size at last scan
        mtime_ms    INTEGER,                   -- file mtime at last scan
        scanned_at  INTEGER                    -- epoch ms of last scan that read bytes
    );

    CREATE TABLE sessions (
        id                INTEGER PRIMARY KEY,
        tool              TEXT    NOT NULL,
        session_id        TEXT    NOT NULL,    -- harness id (Claude Code: file stem)
        source_id         INTEGER REFERENCES sources(id),
        is_subagent       INTEGER NOT NULL DEFAULT 0,
        parent_session_id TEXT,                -- harness id of the parent session (same tool)
        agent_type        TEXT,                -- subagent type when the harness records it
        title             TEXT,                -- first line (<=120 chars) of the opening prompt
        cwd               TEXT,
        started_at_ms     INTEGER,
        ended_at_ms       INTEGER,
        user_turns        INTEGER NOT NULL DEFAULT 0,
        -- filled by later tickets
        model_source      TEXT,                -- 03: 'measured' | 'guessed'
        project           TEXT,                -- 05: git toplevel or cwd
        t3_thread_id      TEXT,                -- 05
        t3_title          TEXT,                -- 05
        label             TEXT,                -- 06: task kind
        label_source      TEXT,                -- 06: 'rule' | 'manual'
        UNIQUE (tool, session_id)
    );
    CREATE INDEX sessions_parent ON sessions (tool, parent_session_id);

    CREATE TABLE requests (
        id                          INTEGER PRIMARY KEY,
        tool                        TEXT    NOT NULL,
        request_key                 TEXT    NOT NULL, -- Claude Code: message.id
        session_id                  TEXT    NOT NULL, -- sessions.session_id (the transcript it came from)
        is_subagent                 INTEGER NOT NULL DEFAULT 0,
        source_id                   INTEGER REFERENCES sources(id),
        ts_ms                       INTEGER NOT NULL, -- epoch ms
        model                       TEXT,             -- raw model name, NULL when absent
        canonical_model             TEXT,             -- 04
        effort                      TEXT,             -- reasoning effort when recorded
        input_tokens                INTEGER NOT NULL DEFAULT 0, -- uncached input
        cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
        cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens               INTEGER NOT NULL DEFAULT 0,
        reasoning_tokens            INTEGER,          -- 02/03 where reported separately
        cost_usd                    REAL,             -- 04
        UNIQUE (tool, request_key)
    );
    CREATE INDEX requests_session ON requests (tool, session_id);
    CREATE INDEX requests_ts ON requests (ts_ms);
    "#,
    // v2 (ticket 02): Codex parser state carried between incremental scans of
    // one rollout file (last cumulative token totals, current model/effort,
    // forked-history cut). JSON shape: `codex::Cursor`.
    r#"
    CREATE TABLE codex_cursors (
        source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
        state     TEXT    NOT NULL
    );
    "#,
    // v3 (ticket 04): a request priced and then changed by a later merge (streamed
    // records grow the output count) loses its price, so the next price step
    // recomputes it.
    r#"
    CREATE TRIGGER requests_unprice
    AFTER UPDATE OF model, input_tokens, cache_read_input_tokens,
                    cache_creation_input_tokens, output_tokens ON requests
    WHEN (NEW.canonical_model IS NOT NULL OR NEW.cost_usd IS NOT NULL)
     AND (OLD.model IS NOT NEW.model
          OR OLD.input_tokens <> NEW.input_tokens
          OR OLD.cache_read_input_tokens <> NEW.cache_read_input_tokens
          OR OLD.cache_creation_input_tokens <> NEW.cache_creation_input_tokens
          OR OLD.output_tokens <> NEW.output_tokens)
    BEGIN
        UPDATE requests SET canonical_model = NULL, cost_usd = NULL WHERE id = NEW.id;
    END;
    "#,
    // v4 (ticket 05): project folder name next to the resolved project path,
    // and lookups by project and by T3 thread id.
    r#"
    ALTER TABLE sessions ADD COLUMN project_name TEXT;
    CREATE INDEX sessions_project ON sessions (project);
    CREATE INDEX sessions_t3_thread ON sessions (t3_thread_id);
    "#,
    // v5 (ticket 06): hand-set task labels. Kept apart from `sessions` so a
    // rebuild that drops and re-reads the raw-log rows cannot lose them.
    r#"
    CREATE TABLE session_labels (
        tool       TEXT    NOT NULL,
        session_id TEXT    NOT NULL,
        label      TEXT    NOT NULL,
        set_at     INTEGER NOT NULL,           -- epoch ms
        PRIMARY KEY (tool, session_id)
    );
    "#,
    // v6: timestamp-only event intervals and cached tree active time. Re-read
    // existing sources once to collect non-request events as well.
    r#"
    ALTER TABLE sessions ADD COLUMN active_ms INTEGER;
    CREATE TABLE session_events (
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        tool TEXT NOT NULL,
        session_id TEXT NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        PRIMARY KEY (source_id, tool, session_id, start_ms, end_ms)
    );
    CREATE INDEX session_events_session ON session_events(tool, session_id, start_ms);
    UPDATE sources SET offset = 0, size = 0, mtime_ms = NULL;
    DELETE FROM codex_cursors;
    UPDATE sessions SET user_turns = 0;
    "#,
    // v7: parents can belong to another harness. Re-read sources containing
    // internal review requests so their timestamp-only timelines are corrected.
    r#"
    ALTER TABLE sessions ADD COLUMN parent_tool TEXT;
    ALTER TABLE sessions ADD COLUMN lineage_source TEXT;
    ALTER TABLE sessions ADD COLUMN lineage_id TEXT;
    CREATE INDEX sessions_lineage_id ON sessions(lineage_id);
    CREATE INDEX sessions_cross_parent ON sessions(parent_tool, parent_session_id);
    UPDATE sources SET offset = 0, size = 0, mtime_ms = NULL
      WHERE id IN (SELECT source_id FROM requests WHERE model = 'codex-auto-review');
    DELETE FROM codex_cursors WHERE source_id IN (SELECT source_id FROM requests WHERE model = 'codex-auto-review');
    DELETE FROM session_events WHERE source_id IN (SELECT source_id FROM requests WHERE model = 'codex-auto-review');
    UPDATE sessions SET user_turns = 0, active_ms = NULL, started_at_ms = NULL, ended_at_ms = NULL
      WHERE source_id IN (SELECT source_id FROM requests WHERE model = 'codex-auto-review');
    DELETE FROM sessions WHERE (tool, session_id) IN (
      SELECT tool, session_id FROM requests GROUP BY tool, session_id
      HAVING SUM(model IS NOT 'codex-auto-review') = 0);
    DELETE FROM requests WHERE model = 'codex-auto-review';
    "#,
];

pub struct Store {
    pub conn: Connection,
}

impl Store {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
        }
        let conn = Connection::open(path).with_context(|| format!("opening {}", path.display()))?;
        Self::init(conn)
    }

    pub fn open_in_memory() -> Result<Self> {
        Self::init(Connection::open_in_memory()?)
    }

    fn init(conn: Connection) -> Result<Self> {
        conn.execute_batch(
            "PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA foreign_keys = ON;",
        )?;
        let mut store = Store { conn };
        store.migrate()?;
        Ok(store)
    }

    fn migrate(&mut self) -> Result<()> {
        let version: usize =
            self.conn
                .query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))? as usize;
        for (i, sql) in MIGRATIONS.iter().enumerate().skip(version) {
            let tx = self.conn.transaction()?;
            tx.execute_batch(sql)
                .with_context(|| format!("schema migration to v{}", i + 1))?;
            if i == 6 {
                let keys = tx
                    .prepare("SELECT tool, session_id FROM sessions WHERE active_ms IS NOT NULL")?
                    .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
                    .collect::<rusqlite::Result<Vec<_>>>()?;
                for (tool, sid) in keys {
                    let active = tree_active_ms(&tx, &tool, &sid)?;
                    tx.execute(
                        "UPDATE sessions SET active_ms = ?3 WHERE tool = ?1 AND session_id = ?2",
                        params![tool, sid, active],
                    )?;
                }
            }
            tx.pragma_update(None, "user_version", (i + 1) as i64)?;
            tx.commit()?;
        }
        Ok(())
    }

    pub fn schema_version(&self) -> Result<i64> {
        Ok(self
            .conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))?)
    }
}

/// What the store remembers about a raw log file.
#[derive(Debug, Clone, Copy)]
pub struct SourceState {
    pub id: i64,
    pub offset: u64,
}

pub fn source_state(conn: &Connection, path: &str) -> Result<Option<SourceState>> {
    Ok(conn
        .query_row(
            "SELECT id, offset FROM sources WHERE path = ?1",
            params![path],
            |r| {
                Ok(SourceState {
                    id: r.get(0)?,
                    offset: r.get::<_, i64>(1)? as u64,
                })
            },
        )
        .optional()?)
}

pub fn ensure_source(tx: &Transaction, tool: &str, path: &str) -> Result<i64> {
    tx.execute(
        "INSERT INTO sources (tool, path) VALUES (?1, ?2) ON CONFLICT(path) DO NOTHING",
        params![tool, path],
    )?;
    Ok(tx.query_row(
        "SELECT id FROM sources WHERE path = ?1",
        params![path],
        |r| r.get(0),
    )?)
}

pub fn update_source(
    tx: &Transaction,
    id: i64,
    offset: u64,
    size: u64,
    mtime_ms: Option<i64>,
    scanned_at_ms: i64,
) -> Result<()> {
    tx.execute(
        "UPDATE sources SET offset = ?2, size = ?3, mtime_ms = ?4, scanned_at = ?5 WHERE id = ?1",
        params![id, offset as i64, size as i64, mtime_ms, scanned_at_ms],
    )?;
    Ok(())
}

/// Session facts gathered from one chunk of a transcript. Merged into the
/// stored row: earliest start, latest end, first non-null title/cwd, summed turns.
#[derive(Debug, Default, Clone)]
pub struct SessionUpdate {
    pub tool: &'static str,
    pub session_id: String,
    pub source_id: i64,
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
    pub agent_type: Option<String>,
    pub title: Option<String>,
    pub cwd: Option<String>,
    pub started_at_ms: Option<i64>,
    pub ended_at_ms: Option<i64>,
    pub user_turns: i64,
}

pub fn upsert_session(tx: &Transaction, s: &SessionUpdate) -> Result<()> {
    tx.execute(
        r#"
        INSERT INTO sessions (tool, session_id, source_id, is_subagent, parent_session_id,
                              agent_type, title, cwd, started_at_ms, ended_at_ms, user_turns)
        VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
        ON CONFLICT(tool, session_id) DO UPDATE SET
            source_id         = COALESCE(sessions.source_id, excluded.source_id),
            is_subagent       = MAX(sessions.is_subagent, excluded.is_subagent),
            parent_session_id = COALESCE(sessions.parent_session_id, excluded.parent_session_id),
            agent_type        = COALESCE(sessions.agent_type, excluded.agent_type),
            title             = COALESCE(sessions.title, excluded.title),
            cwd               = COALESCE(sessions.cwd, excluded.cwd),
            started_at_ms     = MIN(COALESCE(sessions.started_at_ms, excluded.started_at_ms),
                                    COALESCE(excluded.started_at_ms, sessions.started_at_ms)),
            ended_at_ms       = MAX(COALESCE(sessions.ended_at_ms, excluded.ended_at_ms),
                                    COALESCE(excluded.ended_at_ms, sessions.ended_at_ms)),
            user_turns        = sessions.user_turns + excluded.user_turns
        "#,
        params![
            s.tool,
            s.session_id,
            s.source_id,
            s.is_subagent as i64,
            s.parent_session_id,
            s.agent_type,
            s.title,
            s.cwd,
            s.started_at_ms,
            s.ended_at_ms,
            s.user_turns,
        ],
    )?;
    Ok(())
}

/// One billed model call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RequestRow {
    pub request_key: String,
    pub ts_ms: i64,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub input_tokens: i64,
    pub cache_read_input_tokens: i64,
    pub cache_creation_input_tokens: i64,
    pub output_tokens: i64,
}

/// Insert a request, or merge it into an existing row with the same key.
/// Harnesses that stream (Claude Code writes one record per content block)
/// repeat the same usage with growing output counts, so counts merge by MAX.
/// Returns true when a new row was added.
pub fn upsert_request(
    tx: &Transaction,
    tool: &str,
    session_id: &str,
    is_subagent: bool,
    source_id: i64,
    r: &RequestRow,
) -> Result<bool> {
    if r.model.as_deref() == Some("codex-auto-review") {
        return Ok(false);
    }
    let added =
        tx.prepare_cached(
            r#"INSERT INTO requests (tool, request_key, session_id, is_subagent, source_id, ts_ms,
                                     model, effort, input_tokens, cache_read_input_tokens,
                                     cache_creation_input_tokens, output_tokens)
               VALUES (?1, ?2, ?3, MAX(?4, COALESCE((SELECT is_subagent FROM sessions WHERE tool = ?1 AND session_id = ?3), 0)), ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
               ON CONFLICT(tool, request_key) DO NOTHING"#,
        )?
        .execute(params![
            tool,
            r.request_key,
            session_id,
            is_subagent as i64,
            source_id,
            r.ts_ms,
            r.model,
            r.effort,
            r.input_tokens,
            r.cache_read_input_tokens,
            r.cache_creation_input_tokens,
            r.output_tokens,
        ])? == 1;
    if !added {
        tx.prepare_cached(
            r#"UPDATE requests SET
                   input_tokens                = MAX(input_tokens, ?3),
                   cache_read_input_tokens     = MAX(cache_read_input_tokens, ?4),
                   cache_creation_input_tokens = MAX(cache_creation_input_tokens, ?5),
                   output_tokens               = MAX(output_tokens, ?6),
                   model                       = COALESCE(model, ?7),
                   effort                      = COALESCE(effort, ?8)
               WHERE tool = ?1 AND request_key = ?2"#,
        )?
        .execute(params![
            tool,
            r.request_key,
            r.input_tokens,
            r.cache_read_input_tokens,
            r.cache_creation_input_tokens,
            r.output_tokens,
            r.model,
            r.effort,
        ])?;
    }
    Ok(added)
}

/// Drop everything ingested from one source so it can be re-read from byte 0
/// (used when a file shrank or was replaced).
pub fn reset_source(tx: &Transaction, source_id: i64) -> Result<()> {
    let affected = tx
        .prepare(
            "SELECT tool, session_id FROM session_events WHERE source_id = ?1
         UNION SELECT tool, session_id FROM sessions WHERE source_id = ?1",
        )?
        .query_map(params![source_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    tx.execute(
        "DELETE FROM session_events WHERE source_id = ?1",
        params![source_id],
    )?;
    tx.execute(
        "DELETE FROM requests WHERE source_id = ?1",
        params![source_id],
    )?;
    tx.execute(
        "UPDATE sessions SET user_turns = 0, title = NULL, started_at_ms = NULL, ended_at_ms = NULL
         WHERE source_id = ?1",
        params![source_id],
    )?;
    // The re-read derives the rule label again (hand-set labels live elsewhere).
    tx.execute(
        "UPDATE sessions SET label = NULL, label_source = NULL WHERE source_id = ?1",
        params![source_id],
    )?;
    tx.execute(
        "UPDATE sources SET offset = 0, size = 0 WHERE id = ?1",
        params![source_id],
    )?;
    // Even an empty/truncated replacement must invalidate cached ancestors.
    for (tool, sid) in affected {
        record_events(tx, &tool, &sid, source_id, &[])?;
    }
    Ok(())
}

/// Gaps strictly greater than this threshold are idle, in every ingester.
pub const IDLE_GAP_SECONDS: i64 = 15 * 60;

/// Persist only timestamps, then refresh this session and every recorded ancestor.
/// A late child or a second source for a session therefore updates its parent
/// in the same transaction. UNION makes malformed lineage cycles finite.
pub fn record_events(
    tx: &Transaction,
    tool: &str,
    session_id: &str,
    source_id: i64,
    events: &[(i64, i64)],
) -> Result<()> {
    let mut insert =
        tx.prepare_cached("INSERT OR IGNORE INTO session_events VALUES (?1, ?2, ?3, ?4, ?5)")?;
    for &(start, end) in events {
        insert.execute(params![source_id, tool, session_id, start, end.max(start)])?;
    }
    refresh_ancestors(tx, tool, session_id)?;
    Ok(())
}

/// Refresh a changed timeline or a newly imported parent edge.
pub fn refresh_ancestors(tx: &Transaction, tool: &str, session_id: &str) -> Result<()> {
    let ancestors = tx
        .prepare(
            "WITH RECURSIVE ancestors(tool, sid) AS (
            SELECT ?1, ?2 UNION SELECT COALESCE(s.parent_tool, s.tool), s.parent_session_id
            FROM sessions s JOIN ancestors a ON s.tool = a.tool AND s.session_id = a.sid
            WHERE s.parent_session_id IS NOT NULL
        ) SELECT tool, sid FROM ancestors",
        )?
        .query_map(params![tool, session_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for (tool, sid) in ancestors {
        let active = tree_active_ms(tx, &tool, &sid)?;
        tx.execute(
            "UPDATE sessions SET active_ms = ?3 WHERE tool = ?1 AND session_id = ?2",
            params![tool, sid, active],
        )?;
    }
    Ok(())
}

/// Merge overlapping events and retain only gaps at most the idle threshold.
/// Exposed for ingest verification; queries read the cached sessions column.
pub fn tree_active_ms(conn: &Connection, tool: &str, session_id: &str) -> Result<Option<i64>> {
    let mut stmt = conn.prepare(
        "WITH RECURSIVE tree(tool, sid) AS (
            SELECT ?1, ?2 UNION SELECT s.tool, s.session_id FROM sessions s JOIN tree t
            ON s.parent_session_id = t.sid AND COALESCE(s.parent_tool, s.tool) = t.tool
        ) SELECT e.start_ms, e.end_ms FROM session_events e JOIN tree t
          ON e.session_id = t.sid AND e.tool = t.tool ORDER BY e.start_ms, e.end_ms",
    )?;
    let mut rows = stmt.query(params![tool, session_id])?;
    let mut end: Option<i64> = None;
    let mut active = 0;
    while let Some(row) = rows.next()? {
        let start: i64 = row.get(0)?;
        let next: i64 = row.get(1)?;
        if let Some(prev) = end {
            if start - prev <= IDLE_GAP_SECONDS * 1000 {
                active += (next - prev).max(0);
            } else {
                active += next - start;
            }
            end = Some(prev.max(next));
        } else {
            active += next - start;
            end = Some(next);
        }
    }
    Ok(end.map(|_| active))
}

#[cfg(test)]
mod active_migration_tests {
    use super::*;

    #[test]
    fn v7_removes_internal_review_and_refreshes_cached_parent() {
        let conn = Connection::open_in_memory().unwrap();
        for migration in &MIGRATIONS[..6] {
            conn.execute_batch(migration).unwrap();
        }
        conn.pragma_update(None, "user_version", 6).unwrap();
        conn.execute_batch("INSERT INTO sources(id, tool, path, offset, size) VALUES
            (1, 'codex', '/home/demo/parent', 100, 100), (2, 'codex', '/home/demo/review', 100, 100);
            INSERT INTO sessions(tool, session_id, source_id, active_ms) VALUES ('codex', 'parent', 1, 1200000);
            INSERT INTO sessions(tool, session_id, source_id, is_subagent, parent_session_id, active_ms)
                VALUES ('codex', 'review', 2, 1, 'parent', 1080000);
            INSERT INTO session_events VALUES (1, 'codex', 'parent', 0, 0), (1, 'codex', 'parent', 1200000, 1200000),
                (2, 'codex', 'review', 60000, 60000), (2, 'codex', 'review', 600000, 600000), (2, 'codex', 'review', 1140000, 1140000);
            INSERT INTO requests(tool, session_id, source_id, request_key, ts_ms, model)
                VALUES ('codex', 'review', 2, 'review-request', 60000, 'codex-auto-review');
            INSERT INTO session_labels VALUES ('codex', 'parent', 'coding', 0);
            INSERT INTO codex_cursors VALUES (2, '{}');").unwrap();
        let store = Store::init(conn).unwrap();
        assert_eq!(
            store
                .conn
                .query_row(
                    "SELECT active_ms FROM sessions WHERE session_id = 'parent'",
                    [],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
            0
        );
        for table in ["requests", "codex_cursors"] {
            assert_eq!(
                store
                    .conn
                    .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                        .get::<_, i64>(0))
                    .unwrap(),
                0
            );
        }
        assert_eq!(
            store
                .conn
                .query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            store
                .conn
                .query_row("SELECT COUNT(*) FROM session_labels", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            source_state(&store.conn, "/home/demo/review")
                .unwrap()
                .unwrap()
                .offset,
            0
        );
    }

    #[test]
    fn v5_migration_preserves_usage_and_labels_and_requests_timeline_backfill() {
        let conn = Connection::open_in_memory().unwrap();
        for migration in &MIGRATIONS[..5] {
            conn.execute_batch(migration).unwrap();
        }
        conn.pragma_update(None, "user_version", 5).unwrap();
        conn.execute_batch("INSERT INTO sources(id, tool, path, offset, size) VALUES (1, 'codex', '/home/demo/log', 100, 100);
            INSERT INTO sessions(tool, session_id, source_id, user_turns) VALUES ('codex', 'demo', 1, 3);
            INSERT INTO session_labels VALUES ('codex', 'demo', 'coding', 0);
            INSERT INTO requests(tool, session_id, source_id, request_key, ts_ms) VALUES ('codex', 'demo', 1, 'request', 0);
            INSERT INTO codex_cursors VALUES (1, '{}');").unwrap();
        let store = Store::init(conn).unwrap();
        assert_eq!(store.schema_version().unwrap(), 7);
        for table in ["requests", "session_labels", "sessions"] {
            assert_eq!(
                store
                    .conn
                    .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                        .get::<_, i64>(0))
                    .unwrap(),
                1
            );
        }
        assert_eq!(
            source_state(&store.conn, "/home/demo/log")
                .unwrap()
                .unwrap()
                .offset,
            0
        );
        assert_eq!(
            store
                .conn
                .query_row("SELECT COUNT(*) FROM codex_cursors", [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
}
