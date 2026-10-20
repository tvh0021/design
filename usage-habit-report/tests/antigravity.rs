//! Antigravity ingest against SQLite fixtures built here. All content is
//! invented; the protobuf blobs are encoded by hand with the helpers below.

use rusqlite::{Connection, params};
use std::fs;
use std::path::{Path, PathBuf};
use usage_habit_report::antigravity::{self, scan_antigravity};
use usage_habit_report::query;
use usage_habit_report::store::Store;

const PARENT: &str = "aaaaaaaa-0000-4000-8000-000000000001";
const CHILD: &str = "bbbbbbbb-0000-4000-8000-000000000002";
const T3: &str = "cccccccc-0000-4000-8000-000000000003";
const BARE: &str = "dddddddd-0000-4000-8000-000000000004";

// ---- protobuf encoding -------------------------------------------------

fn varint(mut v: u64, out: &mut Vec<u8>) {
    loop {
        let b = (v & 0x7f) as u8;
        v >>= 7;
        if v == 0 {
            out.push(b);
            return;
        }
        out.push(b | 0x80);
    }
}

#[derive(Clone)]
enum F {
    V(u32, u64),
    B(u32, Vec<u8>),
}

fn msg(fields: &[F]) -> Vec<u8> {
    let mut out = Vec::new();
    for f in fields {
        match f {
            F::V(n, v) => {
                varint(u64::from(*n) << 3, &mut out);
                varint(*v, &mut out);
            }
            F::B(n, b) => {
                varint(u64::from(*n) << 3 | 2, &mut out);
                varint(b.len() as u64, &mut out);
                out.extend_from_slice(b);
            }
        }
    }
    out
}

fn s(n: u32, text: &str) -> F {
    F::B(n, text.as_bytes().to_vec())
}

fn m(n: u32, fields: &[F]) -> F {
    F::B(n, msg(fields))
}

/// Timestamp message for `secs` past an arbitrary epoch base, plus 250 ms.
fn ts(n: u32, secs: u64) -> F {
    m(n, &[F::V(1, 1_700_000_000 + secs), F::V(2, 250_000_000)])
}

fn ms(secs: i64) -> i64 {
    (1_700_000_000 + secs) * 1000 + 250
}

struct Usage {
    input: u64,
    output: u64,
    cached: u64,
    reasoning: Option<u64>,
    bot: Option<&'static str>,
}

fn usage(u: &Usage) -> Vec<u8> {
    let mut f = vec![F::V(2, u.input), F::V(3, u.output)];
    if u.cached > 0 {
        f.push(F::V(5, u.cached));
    }
    if let Some(b) = u.bot {
        f.push(s(7, b));
    }
    if let Some(r) = u.reasoning {
        f.push(F::V(9, r));
        f.push(F::V(10, u.output - r));
    }
    msg(&f)
}

fn gen_meta(u: &Usage, secs: u64, selected: Option<&str>) -> Vec<u8> {
    let mut f = vec![
        ts(1, secs),
        F::V(3, 2),
        ts(6, secs + 1),
        ts(7, secs + 4),
        F::B(9, usage(u)),
    ];
    if let Some(sel) = selected {
        f.push(m(24, &[F::V(4, 1000), s(8, sel)]));
    }
    msg(&f)
}

fn prompt_meta(secs: u64) -> Vec<u8> {
    msg(&[ts(1, secs), F::V(3, 4)])
}

fn prompt_payload(text: &str) -> Vec<u8> {
    msg(&[F::V(1, 14), m(19, &[s(2, text), m(3, &[s(1, text)])])])
}

fn gen_metadata_row(u: &Usage, model: &str) -> Vec<u8> {
    msg(&[m(1, &[F::V(3, 7), F::B(4, usage(u)), s(19, model)])])
}

// ---- fixture databases -------------------------------------------------

fn create_db(path: &Path) -> Connection {
    let c = Connection::open(path).unwrap();
    c.execute_batch(
        "CREATE TABLE steps (idx integer, step_type integer NOT NULL DEFAULT 0,
                             status integer NOT NULL DEFAULT 0, metadata blob,
                             step_payload blob, PRIMARY KEY (idx));
         CREATE TABLE gen_metadata (idx integer, data blob, size integer NOT NULL DEFAULT 0,
                                    PRIMARY KEY (idx));
         CREATE TABLE executor_metadata (idx integer, data blob, PRIMARY KEY (idx));
         CREATE TABLE parent_references (idx integer, data blob, PRIMARY KEY (idx));
         CREATE TABLE trajectory_metadata_blob (id text DEFAULT 'main', data blob,
                                                PRIMARY KEY (id));",
    )
    .unwrap();
    c
}

fn step(c: &Connection, idx: i64, ty: i64, meta: &[u8], payload: Option<&[u8]>) {
    c.execute(
        "INSERT OR REPLACE INTO steps (idx, step_type, metadata, step_payload) VALUES (?1, ?2, ?3, ?4)",
        params![idx, ty, meta, payload],
    )
    .unwrap();
}

fn gen_row(c: &Connection, idx: i64, data: &[u8]) {
    c.execute(
        "INSERT INTO gen_metadata (idx, data) VALUES (?1, ?2)",
        params![idx, data],
    )
    .unwrap();
}

fn blob(c: &Connection, data: &[u8]) {
    c.execute(
        "INSERT INTO trajectory_metadata_blob (data) VALUES (?1)",
        params![data],
    )
    .unwrap();
}

const P1: Usage = Usage {
    input: 100,
    output: 50,
    cached: 0,
    reasoning: None,
    bot: Some("bot-p1"),
};
const P2: Usage = Usage {
    input: 10,
    output: 60,
    cached: 900,
    reasoning: Some(20),
    bot: Some("bot-p2"),
};
const C1: Usage = Usage {
    input: 5,
    output: 7,
    cached: 300,
    reasoning: None,
    bot: Some("bot-c1"),
};
const C2: Usage = Usage {
    input: 3,
    output: 4,
    cached: 0,
    reasoning: None,
    bot: None,
};
const T1: Usage = Usage {
    input: 200,
    output: 30,
    cached: 500,
    reasoning: Some(10),
    bot: None,
};
const T2: Usage = Usage {
    input: 20,
    output: 9,
    cached: 700,
    reasoning: Some(4),
    bot: None,
};

struct Fixture {
    _tmp: tempfile::TempDir,
    dirs: Vec<PathBuf>,
    native: PathBuf,
    store: Store,
}

fn native_parent(dir: &Path) {
    let c = create_db(&dir.join(format!("{PARENT}.db")));
    step(
        &c,
        0,
        14,
        &prompt_meta(0),
        Some(&prompt_payload(
            "  \nPlan the demo widget\nwith more detail",
        )),
    );
    step(&c, 1, 15, &gen_meta(&P1, 10, None), None);
    step(&c, 2, 101, &prompt_meta(20), None);
    step(&c, 3, 15, &gen_meta(&P2, 30, None), None);
    // still running: no usage yet
    step(&c, 4, 15, &msg(&[ts(1, 40)]), None);
    gen_row(&c, 0, &gen_metadata_row(&P1, "claude-example-9"));
    gen_row(&c, 1, &gen_metadata_row(&P2, "claude-example-9"));
    blob(
        &c,
        &msg(&[
            s(3, "eeeeeeee-0000-4000-8000-00000000000e"),
            s(7, "file:///home/demo/my%20proj"),
        ]),
    );
}

fn native_child(dir: &Path) {
    let c = create_db(&dir.join(format!("{CHILD}.db")));
    step(
        &c,
        0,
        14,
        &prompt_meta(15),
        Some(&prompt_payload("Read the widget files")),
    );
    step(&c, 1, 15, &gen_meta(&C1, 16, None), None);
    step(&c, 2, 15, &gen_meta(&C2, 20, None), None);
    gen_row(&c, 0, &gen_metadata_row(&C1, "gemini-example-flash"));
    blob(
        &c,
        &msg(&[
            s(3, "ffffffff-0000-4000-8000-00000000000f"),
            m(4, &[s(1, "widget-reader"), s(8, "Reads files")]),
            s(5, PARENT),
            s(6, PARENT),
        ]),
    );
}

fn native_bare(dir: &Path) {
    let c = create_db(&dir.join(format!("{BARE}.db")));
    step(&c, 0, 15, &gen_meta(&C2, 100, None), None);
}

fn t3_conversation(dir: &Path) {
    let c = create_db(&dir.join(format!("{T3}.db")));
    step(
        &c,
        0,
        14,
        &prompt_meta(50),
        Some(&prompt_payload("Back up the demo laptop")),
    );
    step(
        &c,
        1,
        15,
        &gen_meta(&T1, 51, Some("gemini-example-flash-high")),
        None,
    );
    step(
        &c,
        2,
        14,
        &prompt_meta(60),
        Some(&prompt_payload("And restore it")),
    );
    // not in gen_metadata: model from the step's selected model
    step(
        &c,
        3,
        15,
        &gen_meta(&T2, 61, Some("gemini-example-flash-low")),
        None,
    );
    gen_row(&c, 0, &gen_metadata_row(&T1, "gemini-example-flash"));
    fs::write(
        dir.join(format!("{T3}.meta")),
        r#"{"cwd": "/home/demo/t3proj"}"#,
    )
    .unwrap();
}

fn setup() -> Fixture {
    let tmp = tempfile::tempdir().unwrap();
    let native = tmp.path().join("gemini/antigravity/conversations");
    let t3 = tmp
        .path()
        .join("t3/providers/antigravity/acct1/antigravity-acp/conversations");
    fs::create_dir_all(&native).unwrap();
    fs::create_dir_all(&t3).unwrap();
    native_parent(&native);
    native_child(&native);
    native_bare(&native);
    t3_conversation(&t3);
    fs::write(native.join("notes.txt"), "not a database").unwrap();
    let store = Store::open(&tmp.path().join("usage.db")).unwrap();
    Fixture {
        dirs: vec![native.clone(), t3, tmp.path().join("missing")],
        native,
        store,
        _tmp: tmp,
    }
}

type Req = (
    i64,
    i64,
    i64,
    i64,
    Option<i64>,
    Option<String>,
    Option<String>,
    i64,
    i64,
);

fn req(store: &Store, key: &str) -> Req {
    store
        .conn
        .query_row(
            "SELECT input_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                    output_tokens, reasoning_tokens, model, effort, is_subagent, ts_ms
             FROM requests WHERE tool = 'antigravity' AND request_key = ?1",
            [key],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                    r.get(7)?,
                    r.get(8)?,
                ))
            },
        )
        .unwrap()
}

type Sess = (
    i64,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    i64,
    Option<i64>,
    Option<i64>,
);

fn session(store: &Store, id: &str) -> Sess {
    store
        .conn
        .query_row(
            "SELECT is_subagent, parent_session_id, agent_type, title, cwd, model_source,
                    user_turns, started_at_ms, ended_at_ms
             FROM sessions WHERE tool = 'antigravity' AND session_id = ?1",
            [id],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                    r.get(7)?,
                    r.get(8)?,
                ))
            },
        )
        .unwrap()
}

fn count(store: &Store, sql: &str) -> i64 {
    store.conn.query_row(sql, [], |r| r.get(0)).unwrap()
}

fn file_bytes(p: &Path) -> Vec<u8> {
    fs::read(p).unwrap_or_default()
}

// ---- tests -------------------------------------------------------------

#[test]
fn first_scan_reads_both_layouts() {
    let mut f = setup();
    let r = scan_antigravity(&mut f.store, &f.dirs).unwrap();
    let s = &r.stats;
    assert_eq!(s.scan.files_seen, 4);
    assert_eq!(s.scan.files_read, 4);
    assert_eq!(s.scan.sessions_added, 4);
    // parent 2, child 2, bare 1, t3 2
    assert_eq!(s.scan.rows_added, 7);
    assert_eq!(s.generations_without_usage, 1);
    assert_eq!(s.unreadable, 0);
    assert_eq!(s.immutable_opens, 0);
}

#[test]
fn tokens_models_and_effort() {
    let mut f = setup();
    scan_antigravity(&mut f.store, &f.dirs).unwrap();
    let k = |id: &str, idx: i64| format!("{id}:{idx}");
    // (input, cache_read, cache_create, output, reasoning, model, effort, is_subagent, ts)
    assert_eq!(
        req(&f.store, &k(PARENT, 1)),
        (
            100,
            0,
            0,
            50,
            None,
            Some("claude-example-9".into()),
            None,
            0,
            ms(11)
        )
    );
    assert_eq!(
        req(&f.store, &k(PARENT, 3)),
        (
            10,
            900,
            0,
            60,
            Some(20),
            Some("claude-example-9".into()),
            None,
            0,
            ms(31)
        )
    );
    assert_eq!(
        req(&f.store, &k(CHILD, 1)),
        (
            5,
            300,
            0,
            7,
            None,
            Some("gemini-example-flash".into()),
            None,
            1,
            ms(17)
        )
    );
    // no bot id, no selected model, not in gen_metadata: the database's model, guessed
    assert_eq!(
        req(&f.store, &k(CHILD, 2)).5,
        Some("gemini-example-flash".into())
    );
    assert_eq!(
        req(&f.store, &k(T3, 1)),
        (
            200,
            500,
            0,
            30,
            Some(10),
            Some("gemini-example-flash".into()),
            Some("high".into()),
            0,
            ms(52)
        )
    );
    assert_eq!(
        req(&f.store, &k(T3, 3)),
        (
            20,
            700,
            0,
            9,
            Some(4),
            Some("gemini-example-flash".into()),
            Some("low".into()),
            0,
            ms(62)
        )
    );
    assert_eq!(req(&f.store, &k(BARE, 0)).5, None);
}

#[test]
fn sessions_lineage_and_model_source() {
    let mut f = setup();
    scan_antigravity(&mut f.store, &f.dirs).unwrap();
    let p = session(&f.store, PARENT);
    assert_eq!(p.0, 0);
    assert_eq!(p.1, None);
    assert_eq!(p.3.as_deref(), Some("Plan the demo widget"));
    assert_eq!(p.4.as_deref(), Some("/home/demo/my proj"));
    assert_eq!(p.5.as_deref(), Some("measured"));
    assert_eq!(p.6, 1);
    assert_eq!((p.7, p.8), (Some(ms(0)), Some(ms(40))));

    let c = session(&f.store, CHILD);
    assert_eq!(c.0, 1);
    assert_eq!(c.1.as_deref(), Some(PARENT));
    assert_eq!(c.2.as_deref(), Some("widget-reader"));
    assert_eq!(c.5.as_deref(), Some("guessed"));

    let t = session(&f.store, T3);
    assert_eq!(t.0, 0);
    assert_eq!(t.3.as_deref(), Some("Back up the demo laptop"));
    assert_eq!(t.4.as_deref(), Some("/home/demo/t3proj"));
    assert_eq!(t.5.as_deref(), Some("measured"));
    assert_eq!(t.6, 2);

    let b = session(&f.store, BARE);
    assert_eq!(b.5, None);
    assert_eq!(b.3, None);
}

#[test]
fn parent_and_child_totals_are_distinguishable() {
    let mut f = setup();
    scan_antigravity(&mut f.store, &f.dirs).unwrap();
    // A teamwork run: parent session plus every child pointing at it.
    let run = |sub: i64| {
        f.store
            .conn
            .query_row(
                "SELECT SUM(r.output_tokens) FROM requests r
                 JOIN sessions s ON s.tool = r.tool AND s.session_id = r.session_id
                 WHERE r.tool = 'antigravity' AND r.is_subagent = ?2
                   AND (s.session_id = ?1 OR s.parent_session_id = ?1)",
                params![PARENT, sub],
                |r| r.get::<_, i64>(0),
            )
            .unwrap()
    };
    assert_eq!(run(0), 50 + 60);
    assert_eq!(run(1), 7 + 4);
    // Each generation counts once in the summary.
    let sum = query::summary(&f.store.conn, None, 0).unwrap();
    let ag: Vec<_> = sum
        .groups
        .iter()
        .filter(|g| g.tool == "antigravity")
        .collect();
    assert_eq!(ag.iter().map(|g| g.tokens.requests).sum::<i64>(), 7);
    assert_eq!(
        ag.iter().map(|g| g.tokens.output_tokens).sum::<i64>(),
        50 + 60 + 7 + 4 + 4 + 30 + 9
    );
    assert!(ag.iter().any(|g| g.model.is_none()));
}

#[test]
fn rescan_without_changes_reads_nothing() {
    let mut f = setup();
    scan_antigravity(&mut f.store, &f.dirs).unwrap();
    let r = scan_antigravity(&mut f.store, &f.dirs).unwrap();
    assert_eq!(r.stats.scan.files_seen, 4);
    assert_eq!(r.stats.scan.files_read, 0);
    assert_eq!(r.stats.scan.bytes_read, 0);
    assert_eq!(r.stats.scan.rows_added, 0);
}

#[test]
fn rescan_picks_up_new_and_finished_steps() {
    let mut f = setup();
    scan_antigravity(&mut f.store, &f.dirs).unwrap();
    {
        let c = Connection::open(f.native.join(format!("{PARENT}.db"))).unwrap();
        // the running generation finished in place, and a new turn followed
        let done = Usage {
            input: 1,
            output: 2,
            cached: 3,
            reasoning: None,
            bot: Some("bot-p3"),
        };
        step(&c, 4, 15, &gen_meta(&done, 40, None), None);
        gen_row(&c, 2, &gen_metadata_row(&done, "claude-example-9"));
        step(
            &c,
            5,
            14,
            &prompt_meta(70),
            Some(&prompt_payload("Next question")),
        );
        step(&c, 6, 15, &gen_meta(&P1, 71, None), None);
    }
    let r = scan_antigravity(&mut f.store, &f.dirs).unwrap();
    assert_eq!(r.stats.scan.files_read, 1);
    assert_eq!(r.stats.scan.rows_added, 2);
    let p = session(&f.store, PARENT);
    assert_eq!(p.6, 2, "user turns counted once each");
    assert_eq!(p.3.as_deref(), Some("Plan the demo widget"));
    assert_eq!(req(&f.store, &format!("{PARENT}:4")).3, 2);
    assert_eq!(
        count(
            &f.store,
            "SELECT COUNT(*) FROM requests WHERE tool = 'antigravity'"
        ),
        9
    );
}

#[test]
fn shrunk_database_is_reread() {
    let mut f = setup();
    scan_antigravity(&mut f.store, &f.dirs).unwrap();
    {
        let c = Connection::open(f.native.join(format!("{PARENT}.db"))).unwrap();
        c.execute("DELETE FROM steps WHERE idx >= 2", []).unwrap();
        c.execute_batch("VACUUM").unwrap();
    }
    let r = scan_antigravity(&mut f.store, &f.dirs).unwrap();
    assert_eq!(r.stats.scan.files_reset, 1);
    assert_eq!(
        count(
            &f.store,
            &format!("SELECT COUNT(*) FROM requests WHERE session_id = '{PARENT}'")
        ),
        1
    );
    assert_eq!(session(&f.store, PARENT).6, 1);
}

#[test]
fn reads_wal_without_writing() {
    let mut f = setup();
    let db = f.native.join("99999999-0000-4000-8000-000000000009.db");
    let writer = create_db(&db);
    writer
        .execute_batch("PRAGMA journal_mode = WAL; PRAGMA wal_autocheckpoint = 0;")
        .unwrap();
    step(
        &writer,
        0,
        15,
        &gen_meta(&T2, 200, Some("gemini-example-flash")),
        None,
    );
    let wal = PathBuf::from(format!("{}-wal", db.display()));
    assert!(!file_bytes(&wal).is_empty(), "row lives only in the WAL");
    let all: Vec<PathBuf> = fs::read_dir(&f.native)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| !p.to_string_lossy().ends_with("-shm"))
        .collect();
    let before: Vec<Vec<u8>> = all.iter().map(|p| file_bytes(p)).collect();

    let r = scan_antigravity(&mut f.store, &f.dirs).unwrap();
    assert_eq!(r.stats.immutable_opens, 0);
    assert_eq!(
        req(&f.store, "99999999-0000-4000-8000-000000000009:0").5,
        Some("gemini-example-flash".into())
    );
    let after: Vec<Vec<u8>> = all.iter().map(|p| file_bytes(p)).collect();
    assert!(
        before == after,
        "a database or WAL file changed during the scan"
    );
    drop(writer);
}

#[cfg(unix)]
#[test]
fn falls_back_to_immutable_when_wal_blocks() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let src = tmp.path().join("src");
    let ro = tmp.path().join("ro");
    fs::create_dir_all(&src).unwrap();
    fs::create_dir_all(&ro).unwrap();
    let db = src.join(format!("{T3}.db"));
    let writer = create_db(&db);
    step(
        &writer,
        0,
        15,
        &gen_meta(&T1, 0, Some("gemini-example-flash")),
        None,
    );
    writer
        .execute_batch("PRAGMA journal_mode = WAL; PRAGMA wal_autocheckpoint = 0;")
        .unwrap();
    step(
        &writer,
        1,
        15,
        &gen_meta(&T2, 5, Some("gemini-example-flash")),
        None,
    );
    // A WAL with no SHM in a folder we cannot write: mode=ro cannot open it.
    fs::copy(&db, ro.join(format!("{T3}.db"))).unwrap();
    fs::copy(
        format!("{}-wal", db.display()),
        ro.join(format!("{T3}.db-wal")),
    )
    .unwrap();
    drop(writer);
    fs::set_permissions(&ro, fs::Permissions::from_mode(0o555)).unwrap();

    let (conn, immutable) = antigravity::open_readonly(&ro.join(format!("{T3}.db"))).unwrap();
    drop(conn);
    let mut store = Store::open_in_memory().unwrap();
    let r = scan_antigravity(&mut store, std::slice::from_ref(&ro)).unwrap();
    fs::set_permissions(&ro, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(immutable);
    assert_eq!(r.stats.immutable_opens, 1);
    // immutable mode sees only the checkpointed step
    assert_eq!(r.stats.scan.rows_added, 1);
}
