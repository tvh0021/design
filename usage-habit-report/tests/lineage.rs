//! Synthetic T3 and transcript fixtures. No production ids or transcript text.
use rusqlite::{Connection, params};
use std::{fs, io::Write, path::Path};
use usage_habit_report::{agents, codex, project, query, scan, store::Store};

fn t3(path: &Path, resolved: bool) {
    let c = Connection::open(path).unwrap();
    c.execute_batch(
        "CREATE TABLE orchestration_v2_projection_provider_threads (
        provider_thread_id TEXT, thread_id TEXT, provider TEXT, provider_session_id TEXT,
        payload_json TEXT, updated_at TEXT);
        CREATE TABLE orchestration_v2_projection_subagents (
        subagent_id TEXT, thread_id TEXT, child_thread_id TEXT, provider TEXT,
        origin TEXT, started_at TEXT, completed_at TEXT);",
    )
    .unwrap();
    for (thread, provider, id) in [
        ("t-parent", "claudeAgent", "parent"),
        ("t-child", "codex", "child"),
    ] {
        if thread == "t-child" && !resolved {
            continue;
        }
        c.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES (?1, ?1, ?2, 'opaque-provider-session', ?3, '2026-01-01T00:00:00Z')",
            params![thread, provider, serde_json::json!({"nativeThreadRef":{"nativeId":id,"driver":provider}}).to_string()]).unwrap();
    }
    c.execute("INSERT INTO orchestration_v2_projection_subagents VALUES ('delegation', 't-parent', 't-child', 'codex', 'delegated', '2026-01-01T00:01:00Z', '2026-01-01T00:19:00Z')", []).unwrap();
}

fn parent(dir: &Path) {
    fs::create_dir_all(dir).unwrap();
    fs::write(
        dir.join("parent.jsonl"),
        concat!(
            "{\"type\":\"user\",\"timestamp\":\"2026-01-01T00:00:00Z\",\"cwd\":\"/home/demo\"}\n",
            "{\"type\":\"assistant\",\"timestamp\":\"2026-01-01T00:20:00Z\"}\n"
        ),
    )
    .unwrap();
}
fn child(dir: &Path, id: &str, model: &str) -> std::path::PathBuf {
    fs::create_dir_all(dir.join("sessions")).unwrap();
    let p = dir.join(format!("sessions/rollout-2026-01-01T00-01-00-{id}.jsonl"));
    let mut text = format!(
        "{{\"type\":\"session_meta\",\"timestamp\":\"2026-01-01T00:01:00Z\",\"payload\":{{\"id\":\"{id}\",\"cwd\":\"/home/demo\"}}}}\n"
    );
    text += &format!(
        "{{\"type\":\"turn_context\",\"timestamp\":\"2026-01-01T00:01:00Z\",\"payload\":{{\"model\":\"{model}\"}}}}\n"
    );
    for m in [1, 10, 19] {
        text += &format!(
            "{{\"type\":\"token_usage_record\",\"timestamp\":\"2026-01-01T00:{m:02}:00Z\",\"payload\":{{\"usage\":{{\"input_tokens\":10,\"output_tokens\":5}}}}}}\n"
        );
    }
    fs::write(&p, text).unwrap();
    p
}
fn active(s: &Store) -> i64 {
    s.conn
        .query_row(
            "SELECT active_ms FROM sessions WHERE tool = 'claude-code' AND session_id = 'parent'",
            [],
            |r| r.get(0),
        )
        .unwrap()
}

#[test]
fn cross_tool_late_child_refreshes_ancestors_and_counts_project_time_once() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("statev2.sqlite");
    t3(&db, true);
    let cc = dir.path().join("claude");
    parent(&cc);
    let cx = dir.path().join("codex");
    let mut s = Store::open_in_memory().unwrap();
    scan::scan_claude(&mut s, &cc).unwrap();
    project::enrich(&mut s.conn, std::slice::from_ref(&db)).unwrap();
    assert_eq!(active(&s), 0);
    let file = child(&cx, "child", "gpt-6.1-sol");
    codex::scan_codex(&mut s, &cx).unwrap();
    project::enrich(&mut s.conn, std::slice::from_ref(&db)).unwrap();
    assert_eq!(active(&s), 20 * 60_000);
    assert_eq!(
        project::detail(&s.conn, "/home/demo", None, 0)
            .unwrap()
            .project
            .active_seconds,
        20 * 60
    );
    let r = agents::agents_report(&s.conn, None, 0).unwrap();
    assert_eq!(r.totals.cross_tool_links, 1);
    assert_eq!(r.totals.timing_matched_links, 0);
    let filtered = agents::agents_report_for(&s.conn, None, Some("claude-code"), 0).unwrap();
    assert_eq!(filtered.heaviest_fanouts[0].subagent_count, 1);
    assert_eq!(filtered.by_tool[0].parents_with_imported_children, 1);
    assert_eq!(filtered.by_tool[0].cross_tool_links, 1);
    assert_eq!(r.heaviest_fanouts[0].subagent_count, 1);
    assert_eq!(
        agents::session_subagents(&s.conn, "claude-code", "parent", None)
            .unwrap()
            .subagent_count,
        1
    );
    let report = query::session_report(
        &s.conn,
        &query::SessionRef {
            tool: "codex".into(),
            session_id: "child".into(),
            matched_by: query::MatchedBy::SessionId,
        },
        None,
        false,
        0,
    )
    .unwrap();
    assert_eq!(report.parent.unwrap().tool, "claude-code");
    fs::OpenOptions::new().append(true).open(file).unwrap().write_all(b"{\"type\":\"response_item\",\"timestamp\":\"2026-01-01T00:25:00Z\",\"payload\":{\"type\":\"function_call_output\"}}\n").unwrap();
    codex::scan_codex(&mut s, &cx).unwrap();
    assert_eq!(active(&s), 25 * 60_000); // refresh works before another enrich
    let mut rebuilt = Store::open_in_memory().unwrap();
    scan::scan_claude(&mut rebuilt, &cc).unwrap();
    codex::scan_codex(&mut rebuilt, &cx).unwrap();
    project::enrich(&mut rebuilt.conn, &[db]).unwrap();
    assert_eq!(active(&s), active(&rebuilt));
}

#[test]
fn timing_requires_exactly_one_candidate_and_matching_project() {
    for count in [1, 2] {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("statev2.sqlite");
        t3(&db, false);
        let cc = dir.path().join("claude");
        parent(&cc);
        let cx = dir.path().join("codex");
        child(&cx, "child", "gpt-6.1-sol");
        if count == 2 {
            child(&cx, "other", "gpt-6.1-sol");
        }
        let mut s = Store::open_in_memory().unwrap();
        scan::scan_claude(&mut s, &cc).unwrap();
        codex::scan_codex(&mut s, &cx).unwrap();
        project::enrich(&mut s.conn, std::slice::from_ref(&db)).unwrap();
        let r = agents::agents_report(&s.conn, None, 0).unwrap();
        assert_eq!(r.totals.timing_matched_links, usize::from(count == 1));
        if count == 1 {
            child(&cx, "later-candidate", "gpt-6.1-sol");
            codex::scan_codex(&mut s, &cx).unwrap();
        }
        project::enrich(&mut s.conn, &[db]).unwrap();
        assert_eq!(
            agents::agents_report(&s.conn, None, 0)
                .unwrap()
                .totals
                .timing_matched_links,
            usize::from(count == 1)
        );
    }
}

#[test]
fn internal_review_never_creates_a_reportable_session_or_timeline() {
    let dir = tempfile::tempdir().unwrap();
    child(dir.path(), "internal", "codex-auto-review");
    let mut s = Store::open_in_memory().unwrap();
    let result = codex::scan_codex(&mut s, dir.path()).unwrap();
    assert_eq!(result.anomalies.internal_review_requests_excluded, 3);
    codex::scan_codex(&mut s, dir.path()).unwrap();
    for table in ["requests", "sessions", "session_events"] {
        assert_eq!(
            s.conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
    assert_eq!(
        agents::agents_report(&s.conn, None, 0)
            .unwrap()
            .totals
            .top_level_sessions,
        0
    );
}

#[test]
fn timing_rejects_other_projects_and_competing_delegations() {
    for conflicting_parent in [false, true] {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("statev2.sqlite");
        t3(&db, false);
        let cc = dir.path().join("claude");
        parent(&cc);
        let cx = dir.path().join("codex");
        child(&cx, "child", "gpt-6.1-sol");
        let mut s = Store::open_in_memory().unwrap();
        scan::scan_claude(&mut s, &cc).unwrap();
        codex::scan_codex(&mut s, &cx).unwrap();
        if conflicting_parent {
            s.conn.execute("INSERT INTO sessions (tool, session_id, cwd) VALUES ('claude-code', 'second-parent', '/home/demo')", []).unwrap();
            let c = Connection::open(&db).unwrap();
            c.execute("INSERT INTO orchestration_v2_projection_provider_threads VALUES ('p2', 't2', 'claudeAgent', 'opaque', '{\"nativeThreadRef\":{\"nativeId\":\"second-parent\"}}', '2026-01-01T00:00:00Z')", []).unwrap();
            c.execute("INSERT INTO orchestration_v2_projection_subagents VALUES ('d2', 't2', 'unresolved', 'codex', 'delegated', '2026-01-01T00:01:00Z', NULL)", []).unwrap();
        } else {
            s.conn
                .execute(
                    "UPDATE sessions SET cwd = '/home/demo/other' WHERE tool = 'codex'",
                    [],
                )
                .unwrap();
        }
        project::enrich(&mut s.conn, &[db]).unwrap();
        assert_eq!(
            agents::agents_report(&s.conn, None, 0)
                .unwrap()
                .totals
                .cross_tool_links,
            0
        );
    }
}

#[test]
fn native_parent_is_preserved_and_duplicate_t3_evidence_is_idempotent() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("statev2.sqlite");
    t3(&db, true);
    let cc = dir.path().join("claude");
    parent(&cc);
    let cx = dir.path().join("codex");
    child(&cx, "child", "gpt-6.1-sol");
    let mut s = Store::open_in_memory().unwrap();
    scan::scan_claude(&mut s, &cc).unwrap();
    codex::scan_codex(&mut s, &cx).unwrap();
    s.conn.execute("UPDATE sessions SET is_subagent = 1, parent_session_id = 'native-parent' WHERE tool = 'codex'", []).unwrap();
    project::enrich(&mut s.conn, &[db.clone(), db]).unwrap();
    let row: (String, Option<String>) = s
        .conn
        .query_row(
            "SELECT parent_session_id, lineage_source FROM sessions WHERE tool = 'codex'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(row, ("native-parent".into(), None));
}

#[test]
fn internal_events_do_not_extend_a_mixed_sessions_time() {
    let dir = tempfile::tempdir().unwrap();
    let file = child(dir.path(), "mixed", "gpt-6.1-sol");
    fs::OpenOptions::new().append(true).open(file).unwrap().write_all(concat!(
        "{\"type\":\"turn_context\",\"timestamp\":\"2026-01-01T00:25:00Z\",\"payload\":{\"model\":\"codex-auto-review\"}}\n",
        "{\"type\":\"token_usage_record\",\"timestamp\":\"2026-01-01T00:30:00Z\",\"payload\":{\"usage\":{\"input_tokens\":100,\"output_tokens\":100}}}\n").as_bytes()).unwrap();
    let mut s = Store::open_in_memory().unwrap();
    codex::scan_codex(&mut s, dir.path()).unwrap();
    let (active, end): (i64, i64) = s
        .conn
        .query_row("SELECT active_ms, ended_at_ms FROM sessions", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(active, 18 * 60_000);
    assert_eq!(
        end,
        chrono::DateTime::parse_from_rfc3339("2026-01-01T00:19:00Z")
            .unwrap()
            .timestamp_millis()
    );
    assert_eq!(
        s.conn
            .query_row("SELECT COUNT(*) FROM requests", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        3
    );
}

#[test]
fn cli_incremental_scan_and_rebuild_agree_with_t3_lineage() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("statev2.sqlite");
    t3(&db, true);
    let store_path = dir.path().join("usage.sqlite");
    let cc = dir.path().join("claude");
    parent(&cc);
    let cx = dir.path().join("codex");
    let run = |command: &str| {
        let output = std::process::Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
            .args(["--offline", "--db"])
            .arg(&store_path)
            .arg(command)
            .arg("--claude-dir")
            .arg(&cc)
            .arg("--codex-dir")
            .arg(&cx)
            .arg("--antigravity-dir")
            .arg(dir.path().join("no-antigravity"))
            .arg("--t3-state")
            .arg(&db)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    };
    let read = || {
        Connection::open_with_flags(&store_path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap()
            .query_row(
                "SELECT active_ms FROM sessions WHERE session_id = 'parent'",
                [],
                |r| r.get::<_, i64>(0),
            )
            .unwrap()
    };
    run("scan");
    assert_eq!(read(), 0);
    child(&cx, "child", "gpt-6.1-sol");
    run("scan");
    let incremental = read();
    assert_eq!(incremental, 20 * 60_000);
    run("rebuild");
    assert_eq!(read(), incremental);
}
