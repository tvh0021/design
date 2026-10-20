//! Invented timelines only. No real transcript content or identifiers.
use std::{fs, io::Write, path::Path};
use usage_habit_report::{codex, project, query, scan, store::Store};

fn active(store: &Store, tool: &str, id: &str) -> i64 {
    store
        .conn
        .query_row(
            "SELECT active_ms FROM sessions WHERE tool = ?1 AND session_id = ?2",
            [tool, id],
            |r| r.get(0),
        )
        .unwrap()
}

fn event(minute: i64, kind: &str) -> String {
    format!("{{\"type\":\"{kind}\",\"timestamp\":\"2026-01-01T00:{minute:02}:00Z\"}}\n")
}

#[test]
fn claude_all_records_remove_only_long_gaps_and_rescan_matches_rebuild() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("demo.jsonl");
    fs::write(
        &path,
        event(0, "user") + &event(40, "tool_result") + &event(50, "assistant"),
    )
    .unwrap();
    let mut store = Store::open_in_memory().unwrap();
    scan::scan_claude(&mut store, dir.path()).unwrap();
    assert_eq!(active(&store, "claude-code", "demo"), 10 * 60_000);
    fs::OpenOptions::new()
        .append(true)
        .open(&path)
        .unwrap()
        .write_all(b"{\"type\":\"user\",\"timestamp\":\"2026-01-01T00:55:00Z\",\"message\":{\"role\":\"user\",\"content\":\"Demo follow-up\"}}\n")
        .unwrap();
    scan::scan_claude(&mut store, dir.path()).unwrap();
    let mut rebuilt = Store::open_in_memory().unwrap();
    scan::scan_claude(&mut rebuilt, dir.path()).unwrap();
    assert_eq!(active(&store, "claude-code", "demo"), 15 * 60_000);
    assert_eq!(
        active(&store, "claude-code", "demo"),
        active(&rebuilt, "claude-code", "demo")
    );
}

fn codex_file(root: &Path, id: &str, parent: Option<&str>, minutes: &[i64]) -> std::path::PathBuf {
    let sessions = root.join("sessions");
    fs::create_dir_all(&sessions).unwrap();
    let path = sessions.join(format!("rollout-2026-01-01T00-00-00-{id}.jsonl"));
    let parent = parent
        .map(|p| format!(",\"parent_thread_id\":\"{p}\""))
        .unwrap_or_default();
    let mut text = format!(
        "{{\"timestamp\":\"2026-01-01T00:00:00Z\",\"type\":\"session_meta\",\"payload\":{{\"id\":\"{id}\",\"cwd\":\"/home/demo\"{parent}}}}}\n"
    );
    for m in minutes {
        // Spaces and a non-request event exercise the full header path.
        text += &format!(
            "{{ \"timestamp\": \"2026-01-01T00:{m:02}:00Z\", \"type\": \"response_item\", \"payload\": {{\"type\":\"function_call_output\"}} }}\n"
        );
    }
    fs::write(&path, text).unwrap();
    path
}

#[test]
fn codex_all_events_remove_only_long_gaps_and_rescan_matches_rebuild() {
    let dir = tempfile::tempdir().unwrap();
    let path = codex_file(dir.path(), "demo", None, &[40, 50]);
    let mut store = Store::open_in_memory().unwrap();
    codex::scan_codex(&mut store, dir.path()).unwrap();
    assert_eq!(active(&store, "codex", "demo"), 10 * 60_000);
    fs::OpenOptions::new().append(true).open(path).unwrap()
        .write_all(b"{\"timestamp\":\"2026-01-01T00:55:00Z\",\"type\":\"response_item\",\"payload\":{\"type\":\"message\",\"role\":\"user\",\"content\":[{\"type\":\"input_text\",\"text\":\"Demo follow-up\"}]}}\n").unwrap();
    codex::scan_codex(&mut store, dir.path()).unwrap();
    let mut rebuilt = Store::open_in_memory().unwrap();
    codex::scan_codex(&mut rebuilt, dir.path()).unwrap();
    assert_eq!(active(&store, "codex", "demo"), 15 * 60_000);
    assert_eq!(
        active(&store, "codex", "demo"),
        active(&rebuilt, "codex", "demo")
    );
}

#[test]
fn late_child_and_grandchild_bridge_parent_and_project_counts_time_once() {
    let dir = tempfile::tempdir().unwrap();
    fs::create_dir_all(dir.path().join("project")).unwrap();
    let path = dir.path().join("project/parent.jsonl");
    fs::write(&path, event(0, "user") + &event(20, "assistant")).unwrap();
    let mut store = Store::open_in_memory().unwrap();
    scan::scan_claude(&mut store, dir.path()).unwrap();
    assert_eq!(active(&store, "claude-code", "parent"), 0);
    let children = dir.path().join("project/parent/subagents");
    fs::create_dir_all(&children).unwrap();
    let child = children.join("agent-demo.jsonl");
    fs::write(&child, event(1, "user")).unwrap();
    scan::scan_claude(&mut store, dir.path()).unwrap();
    assert_eq!(active(&store, "claude-code", "parent"), 60_000);
    fs::OpenOptions::new()
        .append(true)
        .open(&child)
        .unwrap()
        .write_all((event(10, "tool_result") + &event(19, "assistant")).as_bytes())
        .unwrap();
    scan::scan_claude(&mut store, dir.path()).unwrap();
    assert_eq!(active(&store, "claude-code", "parent"), 20 * 60_000);
    assert_eq!(active(&store, "claude-code", "agent-demo"), 18 * 60_000);
    store
        .conn
        .execute(
            "UPDATE sessions SET project = '/home/demo', project_name = 'demo'",
            [],
        )
        .unwrap();
    let report = project::detail(&store.conn, "/home/demo", None, 0).unwrap();
    assert_eq!(report.project.active_seconds, 20 * 60);
    assert_eq!(report.tools[0].active_seconds, 20 * 60);
    assert_eq!(report.project.idle_gap_seconds, 900);
    let session = query::session_report(
        &store.conn,
        &query::SessionRef {
            tool: "claude-code".into(),
            session_id: "parent".into(),
            matched_by: query::MatchedBy::SessionId,
        },
        None,
        false,
        0,
    )
    .unwrap();
    assert_eq!(session.active_seconds, Some(1200));
    assert_eq!(session.elapsed_seconds, Some(1200));
    let mut rebuilt = Store::open_in_memory().unwrap();
    scan::scan_claude(&mut rebuilt, dir.path()).unwrap();
    assert_eq!(
        active(&store, "claude-code", "parent"),
        active(&rebuilt, "claude-code", "parent")
    );
    // A grandchild linked after the first scans must refresh both ancestors.
    let tx = store.conn.transaction().unwrap();
    tx.execute("INSERT INTO sessions (tool, session_id, is_subagent, parent_session_id) VALUES ('claude-code', 'grandchild', 1, 'agent-demo')", []).unwrap();
    let source: i64 = tx
        .query_row("SELECT id FROM sources LIMIT 1", [], |r| r.get(0))
        .unwrap();
    usage_habit_report::store::record_events(
        &tx,
        "claude-code",
        "grandchild",
        source,
        &[(1767226740000, 1767227640000)],
    )
    .unwrap();
    tx.commit().unwrap();
    assert_eq!(active(&store, "claude-code", "parent"), 34 * 60_000);
    // Orphans count separately even when marked as subagents.
    store.conn.execute("INSERT INTO sessions (tool, session_id, is_subagent, parent_session_id, project, active_ms) VALUES ('claude-code', 'orphan', 1, 'missing', '/home/demo', 60000)", []).unwrap();
    assert_eq!(
        project::detail(&store.conn, "/home/demo", None, 0)
            .unwrap()
            .project
            .active_seconds,
        35 * 60
    );
}

#[test]
fn threshold_overlap_out_of_order_and_reset() {
    let mut store = Store::open_in_memory().unwrap();
    let tx = store.conn.transaction().unwrap();
    let source = usage_habit_report::store::ensure_source(&tx, "codex", "/home/demo/log").unwrap();
    usage_habit_report::store::upsert_session(
        &tx,
        &usage_habit_report::store::SessionUpdate {
            tool: "codex",
            session_id: "demo".into(),
            source_id: source,
            ..Default::default()
        },
    )
    .unwrap();
    usage_habit_report::store::record_events(
        &tx,
        "codex",
        "demo",
        source,
        &[
            (1_800_001, 1_800_001),
            (900_000, 900_000),
            (0, 100),
            (50, 200),
        ],
    )
    .unwrap();
    tx.commit().unwrap();
    assert_eq!(active(&store, "codex", "demo"), 900_000);
    let tx = store.conn.transaction().unwrap();
    usage_habit_report::store::reset_source(&tx, source).unwrap();
    let cleared: Option<i64> = tx
        .query_row(
            "SELECT active_ms FROM sessions WHERE session_id = 'demo'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(cleared, None);
    usage_habit_report::store::record_events(&tx, "codex", "demo", source, &[(0, 1000)]).unwrap();
    tx.commit().unwrap();
    assert_eq!(active(&store, "codex", "demo"), 1000);
}

#[test]
fn codex_fork_copy_does_not_bridge_child_idle_time() {
    let mut chunk = codex::Chunk::new("rollout-demo", Default::default());
    chunk.push_line(br#"{"timestamp":"2026-01-01T00:00:00Z","ordinal":0,"type":"session_meta","payload":{"id":"child","parent_thread_id":"parent","forked_from_id":"parent","subagent_history_start_ordinal":5}}"#);
    chunk.push_line(br#"{"timestamp":"2026-01-01T00:10:00Z","ordinal":1,"type":"response_item","payload":{"type":"function_call_output"}}"#);
    chunk.push_line(br#"{"timestamp":"2026-01-01T00:20:00Z","ordinal":5,"type":"event_msg","payload":{"type":"task_complete"}}"#);
    assert_eq!(chunk.events.len(), 2);
    assert_eq!(chunk.events[1].0 - chunk.events[0].0, 20 * 60_000);
}

#[test]
fn cli_rebuild_matches_scan_after_appended_turn() {
    let dir = tempfile::tempdir().unwrap();
    let logs = dir.path().join("claude");
    fs::create_dir(&logs).unwrap();
    let path = logs.join("demo.jsonl");
    fs::write(
        &path,
        event(0, "user") + &event(40, "assistant") + &event(50, "assistant"),
    )
    .unwrap();
    let db = dir.path().join("store.db");
    let empty = dir.path().join("empty");
    let run = |verb| {
        let out = std::process::Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
            .env("XDG_CONFIG_HOME", dir.path().join("config"))
            .arg("--offline")
            .arg("--db")
            .arg(&db)
            .arg(verb)
            .arg("--claude-dir")
            .arg(&logs)
            .arg("--codex-dir")
            .arg(&empty)
            .arg("--antigravity-dir")
            .arg(&empty)
            .arg("--t3-state")
            .arg(&empty)
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    };
    run("scan");
    fs::OpenOptions::new().append(true).open(&path).unwrap()
        .write_all(b"{\"type\":\"user\",\"timestamp\":\"2026-01-01T00:55:00Z\",\"message\":{\"role\":\"user\",\"content\":\"Demo follow-up\"}}\n").unwrap();
    run("scan");
    let before = active(&Store::open(&db).unwrap(), "claude-code", "demo");
    run("rebuild");
    assert_eq!(before, 15 * 60_000);
    assert_eq!(
        active(&Store::open(&db).unwrap(), "claude-code", "demo"),
        before
    );
}
