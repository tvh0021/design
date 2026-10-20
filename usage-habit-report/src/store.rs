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
    let added =
        tx.prepare_cached(
            r#"INSERT INTO requests (tool, request_key, session_id, is_subagent, source_id, ts_ms,
                                     model, effort, input_tokens, cache_read_input_tokens,
                                     cache_creation_input_tokens, output_tokens)
               VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
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
    tx.execute(
        "DELETE FROM requests WHERE source_id = ?1",
        params![source_id],
    )?;
    tx.execute(
        "UPDATE sessions SET user_turns = 0, title = NULL, started_at_ms = NULL, ended_at_ms = NULL
         WHERE source_id = ?1",
        params![source_id],
    )?;
    tx.execute(
        "UPDATE sources SET offset = 0, size = 0 WHERE id = ?1",
        params![source_id],
    )?;
    Ok(())
}
