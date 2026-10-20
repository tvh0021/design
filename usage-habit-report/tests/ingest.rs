//! Claude Code ingest against hand-written fixtures in tests/fixtures/claude.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use usage_habit_report::query;
use usage_habit_report::scan::scan_claude;
use usage_habit_report::store::Store;

const PARENT: &str = "11111111-1111-4111-8111-111111111111";
const SUB: &str = "agent-a0000000000000001";
const BETA: &str = "22222222-2222-4222-8222-222222222222";

fn copy_dir(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for e in fs::read_dir(src).unwrap() {
        let e = e.unwrap();
        let to = dst.join(e.file_name());
        if e.file_type().unwrap().is_dir() {
            copy_dir(&e.path(), &to);
        } else {
            fs::copy(e.path(), to).unwrap();
        }
    }
}

/// Fresh copy of the fixture tree plus an empty store.
fn setup() -> (tempfile::TempDir, PathBuf, Store) {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("projects");
    copy_dir(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/claude/projects"),
        &root,
    );
    let store = Store::open(&tmp.path().join("usage.db")).unwrap();
    (tmp, root, store)
}

fn parent_file(root: &Path) -> PathBuf {
    root.join("-home-demo-alpha")
        .join(format!("{PARENT}.jsonl"))
}

fn append(path: &Path, text: &str) {
    OpenOptions::new()
        .append(true)
        .open(path)
        .unwrap()
        .write_all(text.as_bytes())
        .unwrap();
}

fn tokens(store: &Store, key: &str) -> (i64, i64, i64, i64) {
    store
        .conn
        .query_row(
            "SELECT input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens
             FROM requests WHERE request_key = ?1",
            [key],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap()
}

#[test]
fn first_scan_reads_everything() {
    let (_tmp, root, mut store) = setup();
    let r = scan_claude(&mut store, &root).unwrap();
    assert_eq!(r.stats.files_seen, 3);
    assert_eq!(r.stats.files_read, 3);
    assert_eq!(r.stats.sessions_added, 3);
    // A1, A2 (parent), S1, S2 (subagent), B1, B2; the synthetic record is skipped
    assert_eq!(r.stats.rows_added, 6);
    assert_eq!(r.stats.bad_lines, 1);
    let total: u64 = walk_sizes(&root);
    assert_eq!(r.stats.bytes_read, total);
}

fn walk_sizes(root: &Path) -> u64 {
    walkdir::WalkDir::new(root)
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.path().extension().is_some_and(|x| x == "jsonl"))
        .map(|e| e.metadata().unwrap().len())
        .sum()
}

#[test]
fn streaming_records_merge_and_all_four_token_kinds_are_kept() {
    let (_tmp, root, mut store) = setup();
    scan_claude(&mut store, &root).unwrap();
    // Two records for msg_demo_A1: output grew from 5 to 50.
    assert_eq!(tokens(&store, "msg_demo_A1"), (10, 0, 500, 50));
    assert_eq!(tokens(&store, "msg_demo_A2"), (2, 500, 100, 20));
    let synth: i64 = store
        .conn
        .query_row(
            "SELECT COUNT(*) FROM requests WHERE request_key = 'msg_demo_synth'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(synth, 0);
    let (model, effort): (Option<String>, Option<String>) = store
        .conn
        .query_row(
            "SELECT model, effort FROM requests WHERE request_key = 'msg_demo_B1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(model, None, "absent model is stored as NULL");
    assert_eq!(effort, None);
}

#[test]
fn sessions_titles_and_subagent_links() {
    let (_tmp, root, mut store) = setup();
    scan_claude(&mut store, &root).unwrap();
    type Row = (
        i64,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        i64,
        i64,
        i64,
    );
    let get = |sid: &str| -> Row {
        store
            .conn
            .query_row(
                "SELECT is_subagent, parent_session_id, agent_type, title, cwd, user_turns,
                        started_at_ms, ended_at_ms
                 FROM sessions WHERE session_id = ?1",
                [sid],
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
                    ))
                },
            )
            .unwrap()
    };

    let p = get(PARENT);
    assert_eq!(p.0, 0);
    assert_eq!(p.1, None);
    // Command and local-command records are skipped; first line only.
    assert_eq!(p.3.as_deref(), Some("Summarise the demo widget"));
    assert_eq!(p.4.as_deref(), Some("/home/demo/alpha"));
    assert_eq!(p.5, 1, "task-notification and tool results are not turns");
    assert!(p.6 < p.7);

    let s = get(SUB);
    assert_eq!(s.0, 1);
    assert_eq!(s.1.as_deref(), Some(PARENT));
    assert_eq!(s.2.as_deref(), Some("Explore"));

    let sub_rows: i64 = store
        .conn
        .query_row(
            "SELECT COUNT(*) FROM requests WHERE session_id = ?1 AND is_subagent = 1",
            [SUB],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(sub_rows, 2);

    let b = get(BETA);
    assert_eq!(b.3.unwrap().chars().count(), 120);
}

#[test]
fn rescan_is_incremental_and_waits_for_complete_lines() {
    let (_tmp, root, mut store) = setup();
    scan_claude(&mut store, &root).unwrap();

    let r = scan_claude(&mut store, &root).unwrap();
    assert_eq!(r.stats.files_read, 0);
    assert_eq!(r.stats.bytes_read, 0);
    assert_eq!(r.stats.rows_added, 0);

    let f = parent_file(&root);
    let line = r#"{"type":"assistant","message":{"id":"msg_demo_A3","model":"claude-demo-1","usage":{"input_tokens":4,"cache_read_input_tokens":600,"cache_creation_input_tokens":0,"output_tokens":8}},"timestamp":"2026-03-01T10:03:00.000Z","sessionId":"11111111-1111-4111-8111-111111111111"}"#;
    let (head, tail) = line.split_at(40);
    append(&f, head);
    let r = scan_claude(&mut store, &root).unwrap();
    assert_eq!(r.stats.bytes_read, 0, "partial line is not consumed");
    assert_eq!(r.stats.rows_added, 0);

    append(&f, &format!("{tail}\n"));
    // A late duplicate of A2 with a larger output count merges into the row.
    let dup = r#"{"type":"assistant","message":{"id":"msg_demo_A2","model":"claude-demo-1","usage":{"input_tokens":2,"cache_read_input_tokens":500,"cache_creation_input_tokens":100,"output_tokens":25}},"timestamp":"2026-03-01T10:01:06.000Z"}"#;
    append(&f, &format!("{dup}\n"));
    let r = scan_claude(&mut store, &root).unwrap();
    assert_eq!(r.stats.files_read, 1);
    assert_eq!(r.stats.bytes_read, (line.len() + 1 + dup.len() + 1) as u64);
    assert_eq!(r.stats.rows_added, 1);
    assert_eq!(tokens(&store, "msg_demo_A3"), (4, 600, 0, 8));
    assert_eq!(tokens(&store, "msg_demo_A2"), (2, 500, 100, 25));

    let r = scan_claude(&mut store, &root).unwrap();
    assert_eq!(r.stats.bytes_read, 0);
}

#[test]
fn shrunken_file_is_reread_without_double_counting() {
    let (_tmp, root, mut store) = setup();
    scan_claude(&mut store, &root).unwrap();
    let f = root.join("-home-demo-beta").join(format!("{BETA}.jsonl"));
    let first_line = fs::read_to_string(&f)
        .unwrap()
        .lines()
        .next()
        .unwrap()
        .to_string();
    fs::write(&f, format!("{first_line}\n")).unwrap();
    let r = scan_claude(&mut store, &root).unwrap();
    assert_eq!(r.stats.files_reset, 1);
    let n: i64 = store
        .conn
        .query_row(
            "SELECT COUNT(*) FROM requests WHERE session_id = ?1",
            [BETA],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 0);
    let turns: i64 = store
        .conn
        .query_row(
            "SELECT user_turns FROM sessions WHERE session_id = ?1",
            [BETA],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(turns, 1);
}

#[test]
fn summary_groups_by_tool_and_model_and_honours_since() {
    let (_tmp, root, mut store) = setup();
    scan_claude(&mut store, &root).unwrap();
    let s = query::summary(&store.conn, None, 0).unwrap();
    assert_eq!(s.totals.requests, 6);
    assert_eq!(s.totals.input_tokens, 10 + 2 + 3 + 1 + 7 + 1);
    assert_eq!(s.totals.cache_read_input_tokens, 500 + 200 + 7);
    assert_eq!(s.totals.cache_creation_input_tokens, 500 + 100 + 200);
    assert_eq!(s.totals.output_tokens, 50 + 20 + 30 + 15 + 3 + 9);
    let find = |m: Option<&str>| {
        s.groups
            .iter()
            .find(|g| g.model.as_deref() == m)
            .unwrap()
            .tokens
            .clone()
    };
    assert_eq!(find(Some("claude-demo-1")).requests, 3);
    assert_eq!(find(Some("claude-demo-2")).requests, 2);
    assert_eq!(find(None).requests, 1);
    assert!(s.groups.iter().all(|g| g.tool == "claude-code"));

    // Since 2026-02-01: drops the January beta session.
    let feb = usage_habit_report::claude::parse_ts_ms("2026-02-01T00:00:00Z").unwrap();
    let s = query::summary(&store.conn, Some(feb), 0).unwrap();
    assert_eq!(s.totals.requests, 4);
    assert_eq!(s.since.as_deref(), Some("2026-02-01T00:00:00Z"));
}

#[test]
fn cli_summary_json_shape() {
    let (tmp, root, _store) = setup();
    let db = tmp.path().join("cli.db");
    let bin = env!("CARGO_BIN_EXE_usage-habit-report");
    let run = |args: &[&str]| {
        let out = std::process::Command::new(bin)
            .args(args)
            .env("XDG_CONFIG_HOME", tmp.path().join("config"))
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap()
    };
    let out = run(&[
        "--db",
        db.to_str().unwrap(),
        "scan",
        "--claude-dir",
        root.to_str().unwrap(),
    ]);
    assert!(out.contains("6 rows added"), "{out}");
    let out = run(&[
        "--db",
        db.to_str().unwrap(),
        "scan",
        "--claude-dir",
        root.to_str().unwrap(),
    ]);
    assert!(out.contains("0 bytes read"), "{out}");

    let out = run(&[
        "--db",
        db.to_str().unwrap(),
        "query",
        "summary",
        "--json",
        "--since",
        "520w",
    ]);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert!(v["generated_at"].is_string());
    assert!(v["since"].is_string());
    for k in [
        "requests",
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    ] {
        assert!(v["totals"][k].is_i64(), "totals.{k}");
        assert!(v["groups"][0][k].is_i64(), "groups[0].{k}");
    }
    assert_eq!(v["groups"][0]["tool"], "claude-code");
    assert!(
        v["groups"]
            .as_array()
            .unwrap()
            .iter()
            .any(|g| g["model"].is_null())
    );
    assert!(tmp.path().join("config/usage-habit-report").is_dir());
}
