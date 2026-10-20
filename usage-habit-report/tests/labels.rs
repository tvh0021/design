//! Task labels and multi-agent metrics against the hand-written fixtures.

use std::fs;
use std::path::{Path, PathBuf};
use usage_habit_report::labels;
use usage_habit_report::scan::scan_claude;
use usage_habit_report::store::Store;

const PARENT: &str = "11111111-1111-4111-8111-111111111111";
const SUB: &str = "agent-a0000000000000001";
const BETA: &str = "22222222-2222-4222-8222-222222222222";

fn fixtures(tool: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(tool)
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

fn setup() -> (tempfile::TempDir, PathBuf, Store) {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("projects");
    copy_dir(&fixtures("claude").join("projects"), &root);
    let store = Store::open(&tmp.path().join("usage.db")).unwrap();
    (tmp, root, store)
}

fn rule_label(store: &Store, sid: &str) -> (Option<String>, Option<String>) {
    store
        .conn
        .query_row(
            "SELECT label, label_source FROM sessions WHERE session_id = ?1",
            [sid],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap()
}

fn effective(store: &Store, sid: &str) -> Option<String> {
    labels::effective_label(&store.conn, "claude-code", sid).unwrap()
}

#[test]
fn top_level_sessions_get_a_rule_label_and_subagents_do_not() {
    let (tmp, root, mut store) = setup();
    scan_claude(&mut store, &root).unwrap();
    assert_eq!(
        rule_label(&store, PARENT),
        (Some("writing".into()), Some("rule".into()))
    );
    assert_eq!(
        rule_label(&store, BETA),
        (Some("other".into()), Some("rule".into()))
    );
    assert_eq!(rule_label(&store, SUB), (None, None));

    // Only the label and the one-line title are stored, never the rest of the prompt.
    let db = tmp.path().join("usage.db");
    for f in [db.clone(), db.with_extension("db-wal")] {
        let bytes = fs::read(&f).unwrap_or_default();
        let hay = String::from_utf8_lossy(&bytes);
        assert!(!hay.contains("second line of the ask"), "{}", f.display());
    }

    // The subagent reports under its parent's label.
    let r = labels::label_report(&store.conn, 0).unwrap();
    let w = r.labels.iter().find(|l| l.label == "writing").unwrap();
    assert_eq!((w.sessions, w.subagent_sessions), (1, 1));
    let want: i64 = store
        .conn
        .query_row(
            "SELECT COUNT(*) FROM requests WHERE session_id IN (?1, ?2)",
            [PARENT, SUB],
            |r| r.get(0),
        )
        .unwrap();
    assert!(want > 2);
    assert_eq!(w.tokens.requests, want, "parent and subagent requests");
}

#[test]
fn manual_label_survives_rescan_and_rebuild() {
    let (_tmp, root, mut store) = setup();
    scan_claude(&mut store, &root).unwrap();
    let (tool, id) = labels::resolve_session(&store.conn, &PARENT[..8]).unwrap();
    assert_eq!((tool.as_str(), id.as_str()), ("claude-code", PARENT));
    let was = labels::set_manual_label(&store.conn, &tool, &id, "review", 1).unwrap();
    assert_eq!(was.as_deref(), Some("writing"));
    assert_eq!(effective(&store, PARENT).as_deref(), Some("review"));
    // The rule result is kept as it was.
    assert_eq!(rule_label(&store, PARENT).0.as_deref(), Some("writing"));

    // Rescan after the log grows.
    let file = root
        .join("-home-demo-alpha")
        .join(format!("{PARENT}.jsonl"));
    let mut text = fs::read_to_string(&file).unwrap();
    text.push_str(r#"{"type":"user","timestamp":"2026-03-01T11:00:00.000Z","message":{"role":"user","content":"Fix the crash"}}"#);
    text.push('\n');
    fs::write(&file, text).unwrap();
    scan_claude(&mut store, &root).unwrap();
    assert_eq!(effective(&store, PARENT).as_deref(), Some("review"));
    assert_eq!(
        rule_label(&store, PARENT).0.as_deref(),
        Some("writing"),
        "first prompt decides"
    );

    // Rebuild: drop every raw-log row, then rescan.
    store
        .conn
        .execute_batch("DELETE FROM requests; DELETE FROM sessions; DELETE FROM sources;")
        .unwrap();
    assert_eq!(rule_label_count(&store), 0);
    scan_claude(&mut store, &root).unwrap();
    assert_eq!(effective(&store, PARENT).as_deref(), Some("review"));
    assert_eq!(rule_label(&store, PARENT).0.as_deref(), Some("writing"));
    // A relabel replaces the earlier hand-set one.
    let was = labels::set_manual_label(&store.conn, "claude-code", PARENT, "other", 2).unwrap();
    assert_eq!(was.as_deref(), Some("review"));
    assert_eq!(effective(&store, PARENT).as_deref(), Some("other"));
    let r = labels::label_report(&store.conn, 0).unwrap();
    assert_eq!(r.labels.iter().map(|l| l.manual_sessions).sum::<i64>(), 1);
}

fn rule_label_count(store: &Store) -> i64 {
    store
        .conn
        .query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))
        .unwrap()
}

#[test]
fn rereading_a_shrunken_file_derives_the_rule_label_again() {
    let (_tmp, root, mut store) = setup();
    scan_claude(&mut store, &root).unwrap();
    assert_eq!(rule_label(&store, BETA).0.as_deref(), Some("other"));
    let file = root.join("-home-demo-beta").join(format!("{BETA}.jsonl"));
    fs::write(
        &file,
        concat!(
            r#"{"type":"user","timestamp":"2026-03-01T12:00:00.000Z","message":{"role":"user","content":"Fix the crash"}}"#,
            "\n"
        ),
    )
    .unwrap();
    scan_claude(&mut store, &root).unwrap();
    assert_eq!(
        rule_label(&store, BETA),
        (Some("debugging".into()), Some("rule".into()))
    );
}

struct Cli {
    tmp: tempfile::TempDir,
    root: PathBuf,
}

impl Cli {
    fn new() -> Self {
        let (tmp, root, store) = setup();
        drop(store);
        Cli { tmp, root }
    }

    fn run(&self, args: &[&str]) -> Result<String, String> {
        let out = std::process::Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
            .arg("--db")
            .arg(self.tmp.path().join("cli.db"))
            .args(args)
            .env("XDG_CONFIG_HOME", self.tmp.path().join("config"))
            .env("HOME", self.tmp.path()) // keep other harnesses' default folders out
            .env_remove("CODEX_HOME")
            .env("USAGE_HABIT_REPORT_OFFLINE", "1")
            .output()
            .unwrap();
        if out.status.success() {
            Ok(String::from_utf8(out.stdout).unwrap())
        } else {
            Err(String::from_utf8_lossy(&out.stderr).into_owned())
        }
    }

    fn scan(&self) -> String {
        self.run(&[
            "scan",
            "--claude-dir",
            self.root.to_str().unwrap(),
            "--codex-dir",
            fixtures("codex").to_str().unwrap(),
        ])
        .unwrap()
    }

    fn json(&self, args: &[&str]) -> serde_json::Value {
        serde_json::from_str(&self.run(args).unwrap()).unwrap()
    }
}

#[test]
fn cli_label_survives_scan_and_feeds_the_queries() {
    let cli = Cli::new();
    cli.scan();
    let out = cli.run(&["label", "1111", "debugging"]).unwrap();
    assert!(
        out.contains("debugging") && out.contains("was writing"),
        "{out}"
    );
    // Bad kind, unknown session, and a subagent are refused.
    assert!(
        cli.run(&["label", "1111", "nonsense"])
            .unwrap_err()
            .contains("unknown label")
    );
    assert!(
        cli.run(&["label", "zzzz", "coding"])
            .unwrap_err()
            .contains("no session")
    );
    assert!(
        cli.run(&["label", SUB, "coding"])
            .unwrap_err()
            .contains("subagent")
    );

    cli.scan();
    let v = cli.json(&["query", "labels", "--json"]);
    let row = |l: &str| {
        v["labels"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["label"] == l)
            .cloned()
            .unwrap_or_default()
    };
    assert_eq!(row("debugging")["manual_sessions"], 1);
    assert_eq!(row("debugging")["sessions"], 1);
    assert_eq!(row("debugging")["subagent_sessions"], 1);
    assert!(
        row("writing").is_null(),
        "the hand-set label replaced the rule label"
    );
    let counts: i64 = v["labels"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["requests"].as_i64().unwrap())
        .sum();
    assert_eq!(counts, v["totals"]["requests"].as_i64().unwrap());

    let s = cli.json(&["query", "summary", "--group-by", "label", "--json"]);
    assert_eq!(s["group_by"], "label");
    let g = s["groups"].as_array().unwrap();
    assert!(
        g.iter()
            .any(|x| x["label"] == "debugging" && x["tool"] == "claude-code")
    );
    assert_eq!(s["totals"]["requests"], v["totals"]["requests"]);
    // The default grouping is unchanged.
    let d = cli.json(&["query", "summary", "--json"]);
    assert!(d["groups"][0]["model"].is_string() || d["groups"][0]["model"].is_null());
    assert_eq!(d["totals"]["requests"], s["totals"]["requests"]);
    let table = cli
        .run(&["query", "summary", "--group-by", "label"])
        .unwrap();
    assert!(table.starts_with("since: all time\nlabel"), "{table}");
}

#[test]
fn cli_query_agents_json_on_fixtures() {
    let cli = Cli::new();
    cli.scan();
    let v = cli.json(&["query", "agents", "--json"]);
    for k in [
        "generated_at",
        "since",
        "coverage",
        "totals",
        "subagent_token_share",
        "histogram",
        "by_tool",
        "model_mix",
        "subagent_efforts",
        "heaviest_fanouts",
    ] {
        assert!(v.get(k).is_some(), "missing {k}");
    }
    for tool in ["claude-code", "codex", "antigravity"] {
        assert!(v["coverage"][tool]["supported"].is_boolean(), "{tool}");
        assert!(
            v["coverage"][tool]["note"]
                .as_str()
                .is_some_and(|n| !n.is_empty())
        );
    }

    let by_tool = |t: &str| {
        v["by_tool"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["tool"] == t)
            .cloned()
            .unwrap()
    };
    // Claude Code fixtures: two top-level sessions, one with one subagent.
    let c = by_tool("claude-code");
    assert_eq!(c["top_level_sessions"], 2);
    assert_eq!(c["sessions_with_subagents"], 1);
    assert_eq!(c["subagent_sessions"], 1);
    assert_eq!(c["unlinked_subagent_sessions"], 0);
    // Codex fixtures: a thread_spawn and a guardian subagent under one parent.
    let x = by_tool("codex");
    assert_eq!(x["sessions_with_subagents"], 1);
    assert_eq!(x["subagent_sessions"], 2);

    let hist = v["histogram"].as_array().unwrap();
    let in_hist: i64 = hist.iter().map(|b| b["sessions"].as_i64().unwrap()).sum();
    assert_eq!(in_hist, v["totals"]["top_level_sessions"].as_i64().unwrap());
    assert_eq!(hist[0]["bucket"], "0");
    assert_eq!(hist.last().unwrap()["max"], serde_json::Value::Null);

    let heavy = v["heaviest_fanouts"].as_array().unwrap();
    assert_eq!(heavy.len(), 2);
    let claude = heavy.iter().find(|h| h["session_id"] == PARENT).unwrap();
    assert_eq!(claude["tool"], "claude-code");
    assert_eq!(claude["title"], "Summarise the demo widget");
    assert_eq!(claude["subagent_count"], 1);
    assert_eq!(claude["max_concurrent_fanout"], 1);
    assert!(claude["subagent_tokens"].as_i64().unwrap() > 0);
    let share = claude["subagent_token_share"].as_f64().unwrap();
    assert!(share > 0.0 && share < 1.0, "{share}");
    assert!(claude["subagent_models"][0]["model"].is_string());
    assert!(claude["subagent_efforts"].is_array());

    let sub_total: i64 = v["model_mix"]["subagent"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["requests"].as_i64().unwrap())
        .sum();
    assert!(
        sub_total >= 2 + 2,
        "claude + codex subagent requests, got {sub_total}"
    );
    assert!(v["subagent_token_share"]["share"].as_f64().unwrap() > 0.0);

    // --since narrows to requests in the window; the fixtures are from 2026-03.
    let recent = cli.json(&["query", "agents", "--json", "--since", "1h"]);
    assert_eq!(recent["totals"]["top_level_sessions"], 0);
    assert!(recent["since"].is_string());
    // The text form prints without error.
    let text = cli.run(&["query", "agents"]).unwrap();
    assert!(
        text.contains("subagents per session") && text.contains("coverage:"),
        "{text}"
    );
}
