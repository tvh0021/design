//! Codex ingest against hand-written fixtures in tests/fixtures/codex.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use usage_habit_report::codex::scan_codex;
use usage_habit_report::query;
use usage_habit_report::scan::scan_claude;
use usage_habit_report::store::Store;

const A: &str = "aaaaaaaa-0000-4000-8000-00000000000a";
const B: &str = "bbbbbbbb-0000-4000-8000-00000000000b";
const C: &str = "cccccccc-0000-4000-8000-00000000000c";
const D: &str = "dddddddd-0000-4000-8000-00000000000d";
const STEM_A: &str = "rollout-2026-03-01T10-00-00-aaaaaaaa-0000-4000-8000-00000000000a";
const STEM_D: &str = "rollout-2026-03-01T10-06-00-dddddddd-0000-4000-8000-00000000000d";

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

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

fn compress(path: &Path) -> PathBuf {
    let data = fs::read(path).unwrap();
    let out = PathBuf::from(format!("{}.zst", path.display()));
    fs::write(&out, zstd::encode_all(&data[..], 3).unwrap()).unwrap();
    fs::remove_file(path).unwrap();
    out
}

/// Fresh copy of the Codex fixture home, with the archived rollout
/// zstd-compressed the way Codex stores older rollouts.
fn setup() -> (tempfile::TempDir, PathBuf, Store) {
    let tmp = tempfile::tempdir().unwrap();
    let home = tmp.path().join("codex");
    copy_dir(&fixtures().join("codex"), &home);
    compress(
        &home
            .join("archived_sessions")
            .join(format!("{STEM_D}.jsonl")),
    );
    let store = Store::open(&tmp.path().join("usage.db")).unwrap();
    (tmp, home, store)
}

fn file_a(home: &Path) -> PathBuf {
    home.join("sessions/2026/03/01")
        .join(format!("{STEM_A}.jsonl"))
}

fn append(path: &Path, text: &str) {
    OpenOptions::new()
        .append(true)
        .open(path)
        .unwrap()
        .write_all(text.as_bytes())
        .unwrap();
}

type Tok = (i64, i64, i64, i64, Option<i64>);

fn tokens(store: &Store, key: &str) -> Tok {
    store
        .conn
        .query_row(
            "SELECT input_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                    output_tokens, reasoning_tokens
             FROM requests WHERE tool = 'codex' AND request_key = ?1",
            [key],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
        )
        .unwrap()
}

fn count(store: &Store, sql: &str, arg: &str) -> i64 {
    store.conn.query_row(sql, [arg], |r| r.get(0)).unwrap()
}

fn requests_of(store: &Store, sid: &str) -> i64 {
    count(
        store,
        "SELECT COUNT(*) FROM requests WHERE tool = 'codex' AND session_id = ?1",
        sid,
    )
}

#[derive(Debug)]
struct Sess {
    is_subagent: i64,
    parent: Option<String>,
    agent_type: Option<String>,
    title: Option<String>,
    cwd: Option<String>,
    turns: i64,
}

fn session(store: &Store, sid: &str) -> Sess {
    store
        .conn
        .query_row(
            "SELECT is_subagent, parent_session_id, agent_type, title, cwd, user_turns
             FROM sessions WHERE tool = 'codex' AND session_id = ?1",
            [sid],
            |r| {
                Ok(Sess {
                    is_subagent: r.get(0)?,
                    parent: r.get(1)?,
                    agent_type: r.get(2)?,
                    title: r.get(3)?,
                    cwd: r.get(4)?,
                    turns: r.get(5)?,
                })
            },
        )
        .unwrap()
}

#[test]
fn first_scan_reads_live_and_archived_rollouts() {
    let (_tmp, home, mut store) = setup();
    let r = scan_codex(&mut store, &home).unwrap();
    let s = &r.report.stats;
    assert_eq!(s.files_seen, 5);
    assert_eq!(s.files_read, 5);
    // A, B (two files), C (fork subagent), D (archived guardian)
    assert_eq!(s.sessions_added, 4);
    assert_eq!(s.rows_added, 3 + 4 + 2 + 1);
    assert_eq!(s.bad_lines, 0);
    // bytes_read counts decompressed bytes for .zst files: the fixture sizes.
    let total: u64 = walkdir::WalkDir::new(fixtures().join("codex"))
        .into_iter()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_type().is_file())
        .map(|e| e.metadata().unwrap().len())
        .sum();
    assert_eq!(s.bytes_read, total);
    assert_eq!(requests_of(&store, D), 1, "archived .zst rollout ingested");
    assert_eq!(r.anomalies.repeated_token_counts, 1);
    assert_eq!(r.anomalies.token_count_without_info, 1);
    assert_eq!(r.anomalies.cached_exceeds_input, 1);
    assert_eq!(r.anomalies.copied_history_skipped, 1);
}

#[test]
fn cached_input_is_subtracted_from_raw_input() {
    let (_tmp, home, mut store) = setup();
    scan_codex(&mut store, &home).unwrap();
    // Raw input 1000 of which 800 cached: 200 uncached + 800 cache read.
    assert_eq!(
        tokens(&store, &format!("{STEM_A}#8")),
        (200, 800, 0, 50, Some(20))
    );
    // Second call: last_token_usage (1500 in, 1200 cached) = cumulative delta.
    assert_eq!(
        tokens(&store, &format!("{STEM_A}#11")),
        (300, 1200, 0, 30, Some(10))
    );
    assert_eq!(
        tokens(&store, &format!("{STEM_A}#17")),
        (100, 400, 0, 20, Some(10))
    );
    // No request row has more input than the raw count it came from.
    let sums: (i64, i64) = store
        .conn
        .query_row(
            "SELECT SUM(input_tokens), SUM(cache_read_input_tokens) FROM requests
             WHERE session_id = ?1",
            [A],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        sums.0 + sums.1,
        3000,
        "uncached + cached = raw cumulative input"
    );
}

#[test]
fn model_and_effort_come_from_turn_context_and_absent_model_is_null() {
    let (_tmp, home, mut store) = setup();
    scan_codex(&mut store, &home).unwrap();
    let get = |key: String| -> (Option<String>, Option<String>) {
        store
            .conn
            .query_row(
                "SELECT model, effort FROM requests WHERE request_key = ?1",
                [key],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap()
    };
    assert_eq!(
        get(format!("{STEM_A}#8")),
        (Some("gpt-demo-1".into()), Some("high".into()))
    );
    assert_eq!(
        get(format!("{STEM_A}#17")),
        (Some("gpt-demo-1".into()), Some("medium".into()))
    );
    let b_null: i64 = count(
        &store,
        "SELECT COUNT(*) FROM requests WHERE session_id = ?1 AND model IS NULL AND effort IS NULL",
        B,
    );
    assert_eq!(
        b_null, 2,
        "B's first file has no turn_context: model stays NULL"
    );
    let b_later: i64 = count(
        &store,
        "SELECT COUNT(*) FROM requests WHERE session_id = ?1 AND model = 'gpt-demo-2'",
        B,
    );
    assert_eq!(b_later, 2);
    // Per-response records are keyed by response id; the second one had a
    // token_count whose cumulative total never moved.
    assert_eq!(tokens(&store, "resp_demo_b1"), (10, 30, 0, 5, Some(2)));
    assert_eq!(tokens(&store, "resp_demo_b2"), (10, 50, 0, 4, Some(1)));
}

#[test]
fn turns_skip_injected_messages_and_titles_unwrap_ide_context() {
    let (_tmp, home, mut store) = setup();
    scan_codex(&mut store, &home).unwrap();
    let a = session(&store, A);
    // AGENTS.md + environment_context and turn_aborted are not turns.
    assert_eq!(a.turns, 2);
    assert_eq!(a.title.as_deref(), Some("Add a demo widget"));
    assert_eq!(a.cwd.as_deref(), Some("/home/demo/gamma"));
    assert_eq!(a.is_subagent, 0);

    // B spans two files; the second (`<id>_<window>`) joins the same session.
    let b = session(&store, B);
    assert_eq!(b.turns, 2, "the <skill> injection is not a turn");
    assert_eq!(b.title.as_deref(), Some("Check the demo logs"));
    assert_eq!(requests_of(&store, B), 4);
}

#[test]
fn subagents_link_to_parent_and_forked_history_is_not_recounted() {
    let (_tmp, home, mut store) = setup();
    scan_codex(&mut store, &home).unwrap();
    let c = session(&store, C);
    assert_eq!(c.is_subagent, 1);
    assert_eq!(c.parent.as_deref(), Some(A));
    assert_eq!(c.agent_type.as_deref(), Some("thread_spawn"));
    assert_eq!(
        c.turns, 0,
        "the copied parent prompt is not the subagent's turn"
    );
    assert_eq!(c.title, None);
    assert_eq!(requests_of(&store, C), 2, "copied token_count is skipped");
    let sub_rows = count(
        &store,
        "SELECT COUNT(*) FROM requests WHERE session_id = ?1 AND is_subagent = 1 AND model = 'gpt-demo-mini'",
        C,
    );
    assert_eq!(sub_rows, 2);
    let stem_c = "rollout-2026-03-01T10-05-00-cccccccc-0000-4000-8000-00000000000c";
    assert_eq!(
        tokens(&store, &format!("{stem_c}#7")),
        (0, 600, 0, 20, Some(10))
    );
    assert_eq!(
        tokens(&store, &format!("{stem_c}#8")),
        (250, 50, 0, 10, Some(0))
    );

    let d = session(&store, D);
    assert_eq!(d.is_subagent, 1);
    assert_eq!(d.parent.as_deref(), Some(A));
    assert_eq!(d.agent_type.as_deref(), Some("guardian"));
    assert_eq!(d.title.as_deref(), Some("Review this demo action."));
}

#[test]
fn rescan_is_incremental_and_keeps_the_cumulative_baseline() {
    let (_tmp, home, mut store) = setup();
    scan_codex(&mut store, &home).unwrap();
    let r = scan_codex(&mut store, &home).unwrap();
    assert_eq!(r.report.stats.files_read, 0);
    assert_eq!(r.report.stats.bytes_read, 0);

    let f = file_a(&home);
    // A repeat of the last cumulative total: no new request.
    let repeat = r#"{"timestamp":"2026-03-01T15:02:06.000Z","ordinal":19,"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":3000,"cached_input_tokens":2400,"output_tokens":100,"reasoning_output_tokens":40},"last_token_usage":{"input_tokens":500,"cached_input_tokens":400,"output_tokens":20,"reasoning_output_tokens":10}}}}"#;
    let next = r#"{"timestamp":"2026-03-01T15:03:00.000Z","ordinal":20,"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":3700,"cached_input_tokens":3000,"output_tokens":130,"reasoning_output_tokens":45},"last_token_usage":{"input_tokens":700,"cached_input_tokens":600,"output_tokens":30,"reasoning_output_tokens":5}}}}"#;
    append(&f, &format!("{repeat}\n"));
    let (head, tail) = next.split_at(50);
    append(&f, head);
    let r = scan_codex(&mut store, &home).unwrap();
    assert_eq!(r.report.stats.rows_added, 0);
    assert_eq!(
        r.report.stats.bytes_read,
        repeat.len() as u64 + 1,
        "partial line waits"
    );
    assert_eq!(
        r.anomalies.repeated_token_counts, 1,
        "baseline survived the scan boundary"
    );

    append(&f, &format!("{tail}\n"));
    let r = scan_codex(&mut store, &home).unwrap();
    assert_eq!(r.report.stats.rows_added, 1);
    assert_eq!(r.anomalies.usage_mismatch, 0);
    assert_eq!(
        tokens(&store, &format!("{STEM_A}#20")),
        (100, 600, 0, 30, Some(5))
    );
    // Model and effort also carry over from the earlier scan.
    let effort: String = store
        .conn
        .query_row(
            "SELECT effort FROM requests WHERE request_key = ?1",
            [format!("{STEM_A}#20")],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(effort, "medium");
}

#[test]
fn compressed_or_archived_rollout_is_not_counted_twice() {
    let (_tmp, home, mut store) = setup();
    scan_codex(&mut store, &home).unwrap();
    let before = query::summary(&store.conn, None, 0).unwrap().totals;
    let turns_before = session(&store, A).turns;

    // Codex compresses A in place, then archives it.
    let zst = compress(&file_a(&home));
    let r = scan_codex(&mut store, &home).unwrap();
    assert_eq!(r.files_relocated, 1);
    assert_eq!(r.report.stats.rows_added, 0);
    assert_eq!(r.report.stats.bytes_read, 0);
    let archived = home
        .join("archived_sessions")
        .join(zst.file_name().unwrap());
    fs::rename(&zst, &archived).unwrap();
    let r = scan_codex(&mut store, &home).unwrap();
    assert_eq!(r.files_relocated, 1);
    assert_eq!(r.report.stats.rows_added, 0);

    assert_eq!(query::summary(&store.conn, None, 0).unwrap().totals, before);
    assert_eq!(session(&store, A).turns, turns_before);
    let sources = count(
        &store,
        "SELECT COUNT(*) FROM sources WHERE tool = 'codex' AND path LIKE ?1",
        &format!("%{STEM_A}%"),
    );
    assert_eq!(sources, 1);

    // Unchanged compressed files are skipped without decompressing.
    let r = scan_codex(&mut store, &home).unwrap();
    assert_eq!(r.report.stats.files_read, 0);
}

#[test]
fn shrunken_rollout_is_reread_without_double_counting() {
    let (_tmp, home, mut store) = setup();
    scan_codex(&mut store, &home).unwrap();
    let f = file_a(&home);
    let keep: String = fs::read_to_string(&f)
        .unwrap()
        .lines()
        .take(9)
        .map(|l| format!("{l}\n"))
        .collect();
    fs::write(&f, keep).unwrap();
    let r = scan_codex(&mut store, &home).unwrap();
    assert_eq!(r.report.stats.files_reset, 1);
    assert_eq!(requests_of(&store, A), 1);
    assert_eq!(session(&store, A).turns, 1);
}

#[test]
fn summary_shows_codex_alongside_claude() {
    let (tmp, home, mut store) = setup();
    let claude = tmp.path().join("projects");
    copy_dir(&fixtures().join("claude/projects"), &claude);
    scan_claude(&mut store, &claude).unwrap();
    scan_codex(&mut store, &home).unwrap();
    let s = query::summary(&store.conn, None, 0).unwrap();
    assert!(s.groups.iter().any(|g| g.tool == "claude-code"));
    let codex: Vec<_> = s.groups.iter().filter(|g| g.tool == "codex").collect();
    let models: Vec<Option<&str>> = codex.iter().map(|g| g.model.as_deref()).collect();
    for m in [
        Some("gpt-demo-1"),
        Some("gpt-demo-mini"),
        Some("gpt-demo-2"),
        Some("codex-demo-review"),
        None,
    ] {
        assert!(models.contains(&m), "{m:?} in {models:?}");
    }
    assert_eq!(codex.iter().map(|g| g.tokens.requests).sum::<i64>(), 10);
    assert_eq!(s.totals.requests, 6 + 10);
}

#[test]
fn cli_scan_reports_codex() {
    let (tmp, home, _store) = setup();
    let db = tmp.path().join("cli.db");
    let empty = tmp.path().join("no-claude");
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
    let scan = [
        "--db",
        db.to_str().unwrap(),
        "scan",
        "--claude-dir",
        empty.to_str().unwrap(),
        "--codex-dir",
        home.to_str().unwrap(),
    ];
    let out = run(&scan);
    assert!(out.contains("codex: 5 files seen, 5 files read"), "{out}");
    assert!(out.contains("10 rows added, 4 sessions added"), "{out}");
    let out = run(&scan);
    assert!(
        out.contains("codex: 5 files seen, 0 files read, 0 bytes read"),
        "{out}"
    );
    let out = run(&["--db", db.to_str().unwrap(), "query", "summary", "--json"]);
    let v: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert!(
        v["groups"]
            .as_array()
            .unwrap()
            .iter()
            .all(|g| g["tool"] == "codex")
    );
}
