//! Invented harness metadata and opening prompts only.
use rusqlite::{Connection, params};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
use usage_habit_report::{
    agents,
    headlines::{self, Sources},
    mcp, query,
    serve::{data, html, routes},
    store::Store,
};

const A: &str = "aaaaaaaa-0000-4000-8000-00000000000a";
const B: &str = "bbbbbbbb-0000-4000-8000-00000000000b";
const C: &str = "cccccccc-0000-4000-8000-00000000000c";
fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}
fn sources() -> Sources {
    Sources {
        codex_state_dir: fixtures().join("codex"),
        codex_session_index: fixtures().join("codex/session_index.jsonl"),
        antigravity_summaries: fixtures().join("antigravity/conversation_summaries.db"),
    }
}
fn seed(s: &Store) {
    for (tool, id, t3) in [
        ("codex", A, Some("T3 demo parser")),
        ("codex", B, None),
        ("codex", C, None),
        ("antigravity", "demo-conversation", None),
        ("claude-code", "plain", None),
        ("codex", "t3-only", Some("T3 demo title")),
        ("claude-code", "empty", None),
    ] {
        s.conn.execute("INSERT INTO sessions (tool, session_id, title, t3_title, started_at_ms, ended_at_ms) VALUES (?1, ?2, ?3, ?4, 1000, 2000)", params![tool, id, (id != "empty").then_some("  Opening\t demo prompt  "), t3]).unwrap();
    }
}
fn titles(s: &Store) -> Vec<(String, Option<String>, String)> {
    s.conn
        .prepare("SELECT session_id, display_title, title_source FROM sessions ORDER BY session_id")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}
#[test]
fn precedence_normalization_and_all_fallbacks() {
    let mut s = Store::open_in_memory().unwrap();
    seed(&s);
    assert_eq!(headlines::refresh(&mut s.conn, &sources()).unwrap(), 0);
    for (id, title, source) in [
        (A, Some("Repair the demo parser"), "codex-name"),
        (B, Some("Review demo helpers"), "codex-title"),
        (C, Some("Check the demo parser"), "codex-name"),
        (
            "demo-conversation",
            Some("Plan the demo dashboard"),
            "antigravity-summary",
        ),
        ("t3-only", Some("T3 demo title"), "t3"),
        ("plain", Some("Opening demo prompt"), "first-line"),
        ("empty", None, "first-line"),
    ] {
        assert!(titles(&s).contains(&(id.into(), title.map(str::to_owned), source.into())));
    }
    // Index headlines outrank weak database titles, while database names win ties.
    let tmp = tempfile::tempdir().unwrap();
    let index = tmp.path().join("session_index.jsonl");
    fs::write(&index, format!("{{\"id\":\"{A}\",\"thread_name\":\"Index demo parser\"}}\n{{\"id\":\"{B}\",\"thread_name\":\"Index demo helpers\"}}\n")).unwrap();
    let mut paths = sources();
    paths.codex_session_index = index;
    assert_eq!(headlines::refresh(&mut s.conn, &paths).unwrap(), 0);
    assert!(titles(&s).contains(&(
        A.into(),
        Some("Repair the demo parser".into()),
        "codex-name".into()
    )));
    assert!(titles(&s).contains(&(
        B.into(),
        Some("Index demo helpers".into()),
        "codex-name".into()
    )));
    let raw: String = s
        .conn
        .query_row(
            "SELECT title FROM sessions WHERE session_id = ?1",
            [A],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(raw, "  Opening\t demo prompt  ");
    let long = format!("  {}\nend", "é".repeat(130));
    assert_eq!(headlines::normalize(&long).unwrap().chars().count(), 120);
}
#[test]
fn highest_numeric_database_schema_fallback_and_missing_sources() {
    let tmp = tempfile::tempdir().unwrap();
    let mut paths = sources();
    paths.codex_state_dir = tmp.path().into();
    for (n, title) in [(9, "Older name"), (10, "Newest name")] {
        let c = Connection::open(tmp.path().join(format!("state_{n}.sqlite"))).unwrap();
        c.execute_batch("CREATE TABLE threads(id TEXT, name TEXT, title TEXT)")
            .unwrap();
        c.execute(
            "INSERT INTO threads VALUES (?1,?2,'Weak title')",
            params![C, title],
        )
        .unwrap();
    }
    let mut s = Store::open_in_memory().unwrap();
    seed(&s);
    assert_eq!(headlines::refresh(&mut s.conn, &paths).unwrap(), 0);
    assert!(titles(&s).contains(&(C.into(), Some("Newest name".into()), "codex-name".into())));
    fs::write(tmp.path().join("state_11.sqlite"), "bad database").unwrap();
    assert_eq!(headlines::refresh(&mut s.conn, &paths).unwrap(), 1);
    assert!(titles(&s).contains(&(
        C.into(),
        Some("Check the demo parser".into()),
        "codex-name".into()
    )));
    fs::remove_file(tmp.path().join("state_11.sqlite")).unwrap();
    Connection::open(tmp.path().join("state_11.sqlite"))
        .unwrap()
        .execute_batch("CREATE TABLE changed_schema(x TEXT)")
        .unwrap();
    assert_eq!(headlines::refresh(&mut s.conn, &paths).unwrap(), 1);
    paths.codex_session_index = tmp.path().join("missing-index");
    paths.antigravity_summaries = tmp.path().join("missing.db");
    assert_eq!(headlines::refresh(&mut s.conn, &paths).unwrap(), 3);
    assert!(!paths.antigravity_summaries.exists());
    assert!(titles(&s).contains(&(A.into(), Some("T3 demo parser".into()), "t3".into())));
}
#[test]
fn v8_migration_preserves_usage_labels_and_cursors() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("v8.db");
    let s = Store::open(&path).unwrap();
    seed(&s);
    s.conn.execute_batch("INSERT INTO sources(id,tool,path,offset) VALUES (1,'codex','/home/demo/rollout.jsonl',123);
        INSERT INTO codex_cursors VALUES (1,'{}');
        INSERT INTO session_labels VALUES ('claude-code','plain','coding',0);
        ALTER TABLE sessions DROP COLUMN display_title; ALTER TABLE sessions DROP COLUMN title_source;
        DROP TABLE limit_samples; PRAGMA user_version=8;").unwrap();
    drop(s);
    let mut s = Store::open(&path).unwrap();
    assert_eq!(s.schema_version().unwrap(), 10);
    assert!(titles(&s).iter().all(|(_, _, source)| !source.is_empty()));
    headlines::refresh(&mut s.conn, &sources()).unwrap();
    // v9 keeps the Codex offset and cursor; v10 then schedules one re-read of
    // Codex and Claude Code sources for their limit readings.
    assert_eq!(
        s.conn
            .query_row("SELECT offset FROM sources", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM session_labels", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM codex_cursors", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
}
fn command(tmp: &Path, args: &[&str]) -> std::process::Output {
    let out = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
        .arg("--db")
        .arg(tmp.join("usage.db"))
        .args(["--offline"])
        .args(args)
        .env("HOME", tmp)
        .env_remove("CODEX_HOME")
        .env("XDG_CONFIG_HOME", tmp.join("config"))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    out
}
#[test]
fn scan_refreshes_rename_without_rollout_reads_and_rebuild_agrees() {
    let tmp = tempfile::tempdir().unwrap();
    let cx = tmp.path().join("codex");
    fs::create_dir_all(cx.join("sessions")).unwrap();
    let rollout = fixtures()
        .join("codex/sessions/2026/03/01")
        .join(format!("rollout-2026-03-01T10-00-00-{A}.jsonl"));
    fs::copy(
        &rollout,
        cx.join("sessions").join(rollout.file_name().unwrap()),
    )
    .unwrap();
    fs::copy(
        fixtures().join("codex/state_5.sqlite"),
        cx.join("state_5.sqlite"),
    )
    .unwrap();
    let config = tmp.path().join("config/usage-habit-report");
    fs::create_dir_all(&config).unwrap();
    fs::write(config.join("config.toml"), format!("[sources]\ncodex_state_dir = {:?}\ncodex_session_index = {:?}\nantigravity_summaries = {:?}\n", cx.to_str().unwrap(), sources().codex_session_index.to_str().unwrap(), sources().antigravity_summaries.to_str().unwrap())).unwrap();
    let cxstr = cx.to_str().unwrap();
    let first = command(tmp.path(), &["scan", "--codex-dir", cxstr]);
    let output = String::from_utf8(first.stdout).unwrap();
    assert!(
        output
            .lines()
            .find(|s| s.starts_with("codex:"))
            .unwrap()
            .contains("1 files read")
    );
    let s = Store::open(&tmp.path().join("usage.db")).unwrap();
    assert_eq!(
        s.conn
            .query_row(
                "SELECT COUNT(*) FROM sessions WHERE tool = 'codex' AND session_id = ?1",
                [A],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        1
    );
    drop(s);
    let c = Connection::open(cx.join("state_5.sqlite")).unwrap();
    c.execute(
        "UPDATE threads SET name = 'Renamed demo parser' WHERE id = ?1",
        [A],
    )
    .unwrap();
    drop(c);
    let out = command(tmp.path(), &["scan", "--codex-dir", cxstr]);
    let output = String::from_utf8(out.stdout).unwrap();
    assert!(
        output
            .lines()
            .find(|s| s.starts_with("codex:"))
            .unwrap()
            .contains("0 files read, 0 bytes read")
    );
    let s = Store::open(&tmp.path().join("usage.db")).unwrap();
    let before = titles(&s);
    drop(s);
    assert!(before.contains(&(
        A.into(),
        Some("Renamed demo parser".into()),
        "codex-name".into()
    )));
    command(tmp.path(), &["rebuild", "--codex-dir", cxstr]);
    assert_eq!(
        before,
        titles(&Store::open(&tmp.path().join("usage.db")).unwrap())
    );
}
#[test]
fn cli_note_is_single_aggregate_line() {
    let tmp = tempfile::tempdir().unwrap();
    let out = command(tmp.path(), &["scan"]);
    let text = String::from_utf8(out.stdout).unwrap();
    assert_eq!(
        text.lines().filter(|l| l.starts_with("headlines:")).count(),
        1
    );
    assert!(text.contains("3 missing or unreadable title sources skipped"));
}
#[test]
fn headline_reaches_cli_mcp_private_lists_and_fanout() {
    let tmp = tempfile::tempdir().unwrap();
    let mut s = Store::open(&tmp.path().join("usage.db")).unwrap();
    seed(&s);
    s.conn
        .execute(
            "UPDATE sessions SET parent_session_id = ?1, is_subagent = 1 WHERE session_id = ?2",
            params![A, C],
        )
        .unwrap();
    for id in [A, C] {
        s.conn.execute("INSERT INTO requests(tool,session_id,request_key,ts_ms,input_tokens,output_tokens,cost_usd) VALUES ('codex',?1,?1,1000,10,10,1)",[id]).unwrap();
    }
    headlines::refresh(&mut s.conn, &sources()).unwrap();
    let found = match query::resolve_session(&s.conn, A).unwrap() {
        query::SessionLookup::Found(f) => f,
        _ => panic!(),
    };
    let report = query::session_report(&s.conn, &found, None, false, 3000).unwrap();
    assert_eq!(
        report.display_title.as_deref(),
        Some("Repair the demo parser")
    );
    let text = query::render_session(&report);
    assert!(text.contains("title       Repair the demo parser"));
    assert!(text.contains("first line  "));
    let out = command(tmp.path(), &["--no-scan", "query", "session", A]);
    assert!(
        String::from_utf8(out.stdout)
            .unwrap()
            .contains("Repair the demo parser")
    );
    let fans = agents::agents_report(&s.conn, None, 3000).unwrap();
    assert_eq!(
        fans.heaviest_fanouts[0].display_title.as_deref(),
        Some("Repair the demo parser")
    );
    assert_eq!(
        data::session_lists(&s.conn, None, None).unwrap().longest[0]
            .title
            .as_deref(),
        Some("Repair the demo parser")
    );
    let page = routes::load_page(
        &s.conn,
        routes::Params {
            window: Some(data::Window::All),
            tool: None,
        },
        3000,
        &chrono::Local,
        None,
    )
    .unwrap();
    let rendered = html::render(&page);
    assert!(rendered.matches("Repair the demo parser").count() >= 2);
    let mut server = mcp::Server::new(s, true, true, Box::new(|_| Ok(())));
    let request = serde_json::json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"usage_session","arguments":{"key":A}}});
    let response: serde_json::Value =
        serde_json::from_str(&server.handle_line(&request.to_string()).unwrap()).unwrap();
    let value: serde_json::Value =
        serde_json::from_str(response["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(value["display_title"], "Repair the demo parser");
    assert_eq!(value["title_source"], "codex-name");
}

#[test]
fn unlabelled_top_level_sessions_take_a_label_from_their_headline() {
    let mut s = Store::open_in_memory().unwrap();
    seed(&s);
    s.conn
        .execute_batch(
            "UPDATE sessions SET label = 'coding', label_source = 'rule' WHERE session_id = 'plain';
             INSERT INTO sessions (tool, session_id, is_subagent, parent_session_id, title)
             VALUES ('codex', 'child', 1, 'bbbbbbbb-0000-4000-8000-00000000000b', 'Review demo helpers');",
        )
        .unwrap();
    headlines::refresh(&mut s.conn, &sources()).unwrap();
    let labels: Vec<(String, Option<String>, Option<String>)> = s
        .conn
        .prepare("SELECT session_id, label, label_source FROM sessions ORDER BY session_id")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    let get = |id: &str| labels.iter().find(|l| l.0 == id).unwrap().clone();
    // A headline such as "Review demo helpers" classifies as review.
    assert_eq!(
        get(B),
        (B.into(), Some("review".into()), Some("headline".into()))
    );
    // Every top-level session with a headline now has a label.
    for id in [A, C, "demo-conversation", "t3-only"] {
        let (_, label, source) = get(id);
        assert!(label.is_some(), "{id}");
        assert_eq!(source.as_deref(), Some("headline"), "{id}");
    }
    // An existing rule label is kept, subagents are skipped, and a session
    // with no headline at all stays unlabelled.
    assert_eq!(get("plain").1.as_deref(), Some("coding"));
    assert_eq!(get("plain").2.as_deref(), Some("rule"));
    assert_eq!(get("child").1, None);
    assert_eq!(get("empty").1, None);
}
