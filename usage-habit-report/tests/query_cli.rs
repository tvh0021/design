//! CLI behaviour of `query session` / `query thread`, the catch-up scan and
//! `--no-scan`, and `rebuild`.

use rusqlite::{Connection, params};
use std::path::{Path, PathBuf};

const ALPHA: &str = "11111111-1111-4111-8111-111111111111";
const SUB: &str = "agent-a0000000000000001";
const CODEX_A: &str = "aaaaaaaa-0000-4000-8000-00000000000a";

struct Out {
    code: i32,
    stdout: String,
    stderr: String,
}

impl Out {
    fn json(&self) -> serde_json::Value {
        serde_json::from_str(&self.stdout).unwrap_or_else(|e| panic!("{e}: {}", self.stdout))
    }
}

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn run(tmp: &Path, args: &[&str]) -> Out {
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
        .arg("--db")
        .arg(tmp.join("usage.db"))
        .args(args)
        .env("XDG_CONFIG_HOME", tmp.join("config"))
        .env("HOME", tmp) // keep the real harness and T3 folders out
        .env_remove("CODEX_HOME")
        .env("USAGE_HABIT_REPORT_OFFLINE", "1")
        .output()
        .unwrap();
    Out {
        code: out.status.code().unwrap_or(-1),
        stdout: String::from_utf8(out.stdout).unwrap(),
        stderr: String::from_utf8(out.stderr).unwrap(),
    }
}

fn ok(tmp: &Path, args: &[&str]) -> Out {
    let o = run(tmp, args);
    assert_eq!(o.code, 0, "{}", o.stderr);
    o
}

/// A T3 state database with one thread driving the Codex fixture session `a`.
fn t3_state(path: &Path) {
    let c = Connection::open(path).unwrap();
    c.execute_batch(
        r#"CREATE TABLE provider_session_runtime (
               thread_id TEXT PRIMARY KEY, provider_name TEXT NOT NULL,
               resume_cursor_json TEXT);
           CREATE TABLE projection_threads (
               thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
               title TEXT NOT NULL, worktree_path TEXT);
           CREATE TABLE projection_projects (
               project_id TEXT PRIMARY KEY, title TEXT NOT NULL,
               workspace_root TEXT NOT NULL);
           INSERT INTO projection_projects VALUES ('p1', 'Gamma', '/home/demo/gamma');"#,
    )
    .unwrap();
    c.execute(
        "INSERT INTO provider_session_runtime VALUES ('t3-demo-thread', 'codex', ?1)",
        params![format!(r#"{{"threadId":"{CODEX_A}"}}"#)],
    )
    .unwrap();
    c.execute(
        "INSERT INTO projection_threads VALUES ('t3-demo-thread', 'p1', 'Demo thread', NULL)",
        [],
    )
    .unwrap();
}

fn scan_args(tmp: &Path, cmd: &'static str) -> Vec<String> {
    let f = fixtures();
    [
        cmd,
        "--claude-dir",
        f.join("claude/projects").to_str().unwrap(),
        "--codex-dir",
        f.join("codex").to_str().unwrap(),
        "--antigravity-dir",
        tmp.join("no-antigravity").to_str().unwrap(),
        "--t3-state",
        tmp.join("state.sqlite").to_str().unwrap(),
    ]
    .map(String::from)
    .to_vec()
}

fn run_vec(tmp: &Path, args: &[String]) -> Out {
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    ok(tmp, &refs)
}

/// A store holding the fixtures, a T3 link on Codex session `a`, and one extra
/// Claude session id that shares the `11111111` prefix with `ALPHA`.
fn seeded() -> tempfile::TempDir {
    let tmp = tempfile::tempdir().unwrap();
    t3_state(&tmp.path().join("state.sqlite"));
    run_vec(tmp.path(), &scan_args(tmp.path(), "scan"));
    Connection::open(tmp.path().join("usage.db"))
        .unwrap()
        .execute(
            "INSERT INTO sessions (tool, session_id, title) VALUES
             ('claude-code', '11111111-1111-4111-8111-1111111111ff', 'Twin')",
            [],
        )
        .unwrap();
    tmp
}

#[test]
fn session_by_exact_id_json_has_the_documented_shape() {
    let tmp = seeded();
    let o = ok(tmp.path(), &["query", "session", ALPHA, "--json"]);
    let v = o.json();
    assert_eq!(v["matched_by"], "session_id");
    assert_eq!(v["tool"], "claude-code");
    assert_eq!(v["session_id"], ALPHA);
    assert_eq!(v["project_name"], "alpha");
    assert_eq!(v["is_subagent"], false);
    assert_eq!(v["label"], "writing");
    assert_eq!(v["label_source"], "rule");
    assert!(v["rates_fetched_at"].is_string());
    assert!(v["started_at"].is_string() && v["ended_at"].is_string());
    assert!(v["elapsed_seconds"].is_i64());
    assert!(v["rows"].is_null());
    let usage = &v["usage"];
    for k in [
        "requests",
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "unpriced_requests",
    ] {
        assert!(usage[k].is_i64(), "{k}: {usage}");
    }
    assert!(usage.get("reasoning_tokens").is_some());
    assert!(usage.get("estimated_cost_usd").is_some());
    let models = v["models"].as_array().unwrap();
    let by_model: i64 = models.iter().map(|m| m["requests"].as_i64().unwrap()).sum();
    assert_eq!(by_model, usage["requests"].as_i64().unwrap());
    // The fixture's subagent hangs off this session.
    let sub = &v["subagents"];
    assert_eq!(sub["subagent_count"], 1);
    assert_eq!(sub["max_concurrent_fanout"], 1);
    assert!(sub["subagent_token_share"].as_f64().unwrap() > 0.0);
    assert!(!sub["subagent_models"].as_array().unwrap().is_empty());
    // Only the title is stored; none of the prompt's later lines may appear.
    assert!(!o.stdout.contains("second line of the ask"));
}

#[test]
fn session_totals_agree_with_summary_and_agents() {
    let tmp = seeded();
    let sess = ok(tmp.path(), &["query", "session", ALPHA, "--json"]).json();
    let sub = ok(tmp.path(), &["query", "session", SUB, "--json"]).json();
    assert_eq!(sub["is_subagent"], true);
    assert_eq!(sub["parent_session_id"], ALPHA);
    assert!(sub["subagents"].is_null());
    assert_eq!(
        sub["label"], sess["label"],
        "a subagent reports its root's label"
    );
    let total =
        sess["usage"]["requests"].as_i64().unwrap() + sub["usage"]["requests"].as_i64().unwrap();
    let all = ok(tmp.path(), &["query", "project", "alpha", "--json"]).json();
    let claude = all["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["tool"] == "claude-code")
        .unwrap();
    assert_eq!(claude["requests"].as_i64().unwrap(), total);
    let agents = ok(tmp.path(), &["query", "agents", "--json"]).json();
    let h = agents["heaviest_fanouts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|h| h["session_id"] == ALPHA)
        .unwrap();
    assert_eq!(h["subagent_count"], sess["subagents"]["subagent_count"]);
    assert_eq!(h["subagent_tokens"], sess["subagents"]["subagent_tokens"]);
    assert_eq!(h["total_tokens"], sess["subagents"]["total_tokens"]);
}

#[test]
fn rows_adds_one_entry_per_request() {
    let tmp = seeded();
    let v = ok(tmp.path(), &["query", "session", ALPHA, "--rows", "--json"]).json();
    let rows = v["rows"].as_array().unwrap();
    assert_eq!(rows.len() as i64, v["usage"]["requests"].as_i64().unwrap());
    let out: i64 = rows
        .iter()
        .map(|r| r["output_tokens"].as_i64().unwrap())
        .sum();
    assert_eq!(out, v["usage"]["output_tokens"].as_i64().unwrap());
    let r = &rows[0];
    for k in [
        "ts",
        "model",
        "effort",
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "estimated_cost_usd",
    ] {
        assert!(r.get(k).is_some(), "{k} missing: {r}");
    }
    let ts: Vec<&str> = rows.iter().map(|r| r["ts"].as_str().unwrap()).collect();
    assert!(ts.windows(2).all(|w| w[0] <= w[1]));

    // The table form prints the per-request header too.
    let t = ok(tmp.path(), &["query", "session", ALPHA, "--rows"]).stdout;
    assert!(t.contains("cache_create") && t.contains("est. cost"), "{t}");
    assert!(t.contains("session     11111111"), "{t}");
}

#[test]
fn since_narrows_requests_and_rows() {
    let tmp = seeded();
    // The fixtures are from 2026-03; this window is long past them, so nothing falls in it.
    let v = ok(
        tmp.path(),
        &[
            "query", "session", ALPHA, "--rows", "--json", "--since", "1h",
        ],
    )
    .json();
    assert_eq!(v["usage"]["requests"], 0);
    assert_eq!(v["rows"].as_array().unwrap().len(), 0);
    assert_eq!(v["subagents"]["subagent_count"], 0);
    assert!(v["since"].is_string());
}

#[test]
fn prefix_thread_id_and_thread_command() {
    let tmp = seeded();
    let v = ok(tmp.path(), &["query", "session", "aaaaaaaa", "--json"]).json();
    assert_eq!(v["matched_by"], "session_id_prefix");
    assert_eq!(v["session_id"], CODEX_A);
    assert_eq!(v["t3_thread_id"], "t3-demo-thread");
    assert_eq!(v["t3_title"], "Demo thread");
    assert_eq!(v["project"], "/home/demo/gamma");

    let by_key = ok(
        tmp.path(),
        &["query", "session", "t3-demo-thread", "--json"],
    )
    .json();
    assert_eq!(by_key["matched_by"], "t3_thread_id");
    assert_eq!(by_key["session_id"], CODEX_A);

    let th = ok(tmp.path(), &["query", "thread", "t3-demo-thread", "--json"]).json();
    assert_eq!(th["matched_by"], "t3_thread_id");
    assert_eq!(th["usage"], by_key["usage"]);

    // `thread` matches only T3 thread ids: not a session id, not a prefix.
    for key in [CODEX_A, "t3-demo"] {
        let o = run(tmp.path(), &["query", "thread", key, "--json"]);
        assert_eq!(o.code, 1, "{key}");
        assert_eq!(o.json()["error"], "not found");
    }
}

#[test]
fn ambiguous_exits_2_with_candidates_and_not_found_exits_1() {
    let tmp = seeded();
    let o = run(tmp.path(), &["query", "session", "11111111", "--json"]);
    assert_eq!(o.code, 2, "{}", o.stderr);
    let v = o.json();
    assert_eq!(v["error"], "ambiguous");
    assert_eq!(v["key"], "11111111");
    let c = v["candidates"].as_array().unwrap();
    assert_eq!(c.len(), 2);
    assert!(c.iter().any(|x| x["session_id"] == ALPHA));
    assert!(c.iter().all(|x| x["matched_by"] == "session_id_prefix"));
    assert!(o.stderr.contains("ambiguous"));

    let t = run(tmp.path(), &["query", "session", "11111111"]);
    assert_eq!(t.code, 2);
    assert!(
        t.stdout.contains("Twin") && t.stdout.contains(ALPHA),
        "{}",
        t.stdout
    );

    let o = run(tmp.path(), &["query", "session", "zzzz", "--json"]);
    assert_eq!(o.code, 1);
    assert_eq!(o.json()["error"], "not found");
    assert!(o.stderr.contains("not found"));
    let o = run(tmp.path(), &["query", "session", "zzzz"]);
    assert_eq!(o.code, 1);
    assert!(o.stdout.is_empty());
}

#[test]
fn catch_up_reads_new_logs_and_no_scan_skips_it() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join(".claude/projects/-home-demo-alpha");
    std::fs::create_dir_all(&dir).unwrap();
    let log = fixtures()
        .join("claude/projects/-home-demo-alpha")
        .join(format!("{ALPHA}.jsonl"));
    std::fs::copy(&log, dir.join(format!("{ALPHA}.jsonl"))).unwrap();
    let requests = |args: &[&str]| -> (i64, String) {
        let o = ok(tmp.path(), args);
        (o.json()["totals"]["requests"].as_i64().unwrap(), o.stderr)
    };

    let (n, err) = requests(&["--no-scan", "query", "summary", "--json"]);
    assert_eq!((n, err.as_str()), (0, ""));

    let (n, err) = requests(&["query", "summary", "--json"]);
    assert!(n > 0);
    assert!(err.starts_with("scan: 1 files read"), "{err}");
    // stdout is still pure JSON, and a second run has nothing to say.
    let (n2, err) = requests(&["query", "summary", "--json"]);
    assert_eq!((n2, err.as_str()), (n, ""));
}

#[test]
fn rebuild_keeps_a_manual_label_and_rereads_everything() {
    let tmp = seeded();
    let before = ok(tmp.path(), &["query", "summary", "--json"]).json();
    ok(tmp.path(), &["label", ALPHA, "debugging"]);

    let mut args = scan_args(tmp.path(), "rebuild");
    args.push("--no-scan".into()); // global flag: harmless, must still parse
    let o = run_vec(tmp.path(), &args);
    assert!(o.stdout.contains("rebuild: cleared"), "{}", o.stdout);
    assert!(o.stdout.contains("claude-code:"), "{}", o.stdout);

    let v = ok(tmp.path(), &["query", "session", ALPHA, "--json"]).json();
    assert_eq!(v["label"], "debugging");
    assert_eq!(v["label_source"], "manual");
    // The twin row was not in any raw log, so the rebuild dropped it; the
    // fixtures' own totals are back.
    let after = ok(tmp.path(), &["query", "summary", "--json"]).json();
    assert_eq!(after["totals"], before["totals"]);
    let o = run(tmp.path(), &["query", "session", "11111111", "--json"]);
    assert_eq!(o.code, 0, "the twin is gone, so the prefix is unique again");
}
