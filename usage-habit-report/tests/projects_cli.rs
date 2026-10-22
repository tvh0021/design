//! CLI behaviour of project resolution and T3 enrichment: `scan` fills
//! projects and T3 links, `query project` / `query projects` report them, and
//! lookups that miss or match several folders exit 1 and 2.

use rusqlite::{Connection, params};
use std::path::{Path, PathBuf};

const CODEX_A: &str = "aaaaaaaa-0000-4000-8000-00000000000a";

struct Out {
    code: i32,
    stdout: String,
    stderr: String,
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

fn ok(tmp: &Path, args: &[&str]) -> String {
    let o = run(tmp, args);
    assert_eq!(o.code, 0, "{}", o.stderr);
    o.stdout
}

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
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

fn scan(tmp: &Path, t3: &Path) -> String {
    let f = fixtures();
    ok(
        tmp,
        &[
            "scan",
            "--claude-dir",
            f.join("claude/projects").to_str().unwrap(),
            "--codex-dir",
            f.join("codex").to_str().unwrap(),
            "--antigravity-dir",
            tmp.join("no-antigravity").to_str().unwrap(),
            "--t3-state",
            t3.to_str().unwrap(),
        ],
    )
}

#[test]
fn scan_enriches_and_queries_report_projects() {
    let tmp = tempfile::tempdir().unwrap();
    let t3 = tmp.path().join("state.sqlite");
    t3_state(&t3);
    let out = scan(tmp.path(), &t3);
    assert!(
        out.contains("t3: 1 databases read, 1 sessions linked"),
        "{out}"
    );
    assert!(out.contains("0 without a directory"), "{out}");
    let out = scan(tmp.path(), &t3);
    assert!(out.contains("enrich: 0 sessions given a project"), "{out}");

    let db = Connection::open(tmp.path().join("usage.db")).unwrap();
    let (thread, title): (String, String) = db
        .query_row(
            "SELECT t3_thread_id, t3_title FROM sessions WHERE tool = 'codex' AND session_id = ?1",
            params![CODEX_A],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        (thread.as_str(), title.as_str()),
        ("t3-demo-thread", "Demo thread")
    );

    let v: serde_json::Value =
        serde_json::from_str(&ok(tmp.path(), &["query", "projects", "--json"])).unwrap();
    assert!(v["rates_fetched_at"].is_string());
    let projects = v["projects"].as_array().unwrap();
    let names: Vec<&str> = projects.iter().filter_map(|p| p["name"].as_str()).collect();
    for n in ["alpha", "beta", "gamma", "delta"] {
        assert!(names.contains(&n), "{names:?}");
    }
    let sum: i64 = projects
        .iter()
        .map(|p| p["requests"].as_i64().unwrap())
        .sum();
    let summary: serde_json::Value =
        serde_json::from_str(&ok(tmp.path(), &["query", "summary", "--json"])).unwrap();
    assert_eq!(sum, summary["totals"]["requests"].as_i64().unwrap());

    let v: serde_json::Value =
        serde_json::from_str(&ok(tmp.path(), &["query", "project", "GAMMA", "--json"])).unwrap();
    assert_eq!(v["project"], "/home/demo/gamma");
    assert_eq!(v["name"], "gamma");
    assert!(v["sessions"].as_i64().unwrap() >= 1);
    let tools = v["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 1);
    assert_eq!(tools[0]["tool"], "codex");
    assert_eq!(tools[0]["requests"], v["requests"]);
    for k in [
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "estimated_cost_usd",
        "unpriced_requests",
        "first_request_at",
        "last_request_at",
    ] {
        assert!(v.get(k).is_some(), "missing {k}: {v}");
    }

    let v: serde_json::Value = serde_json::from_str(&ok(
        tmp.path(),
        &["query", "project", "/home/demo/alpha", "--json"],
    ))
    .unwrap();
    assert_eq!(v["tools"][0]["tool"], "claude-code");

    let table = ok(tmp.path(), &["query", "project", "alpha"]);
    assert!(table.contains("project: /home/demo/alpha"), "{table}");
    assert!(table.contains("TOTAL"), "{table}");
    let table = ok(tmp.path(), &["query", "projects", "--since", "520w"]);
    assert!(table.contains("/home/demo/gamma"), "{table}");
}

#[test]
fn project_lookup_exit_codes() {
    let tmp = tempfile::tempdir().unwrap();
    let missing_t3 = tmp.path().join("no-t3.sqlite");
    scan(tmp.path(), &missing_t3);
    let db = Connection::open(tmp.path().join("usage.db")).unwrap();
    db.execute(
        "INSERT INTO sessions (tool, session_id, project, project_name)
         VALUES ('codex', 'other-alpha', '/srv/elsewhere/Alpha', 'Alpha')",
        [],
    )
    .unwrap();

    let o = run(tmp.path(), &["query", "project", "alpha", "--json"]);
    assert_eq!(o.code, 2, "{}", o.stderr);
    let v: serde_json::Value = serde_json::from_str(&o.stdout).unwrap();
    assert_eq!(v["error"], "ambiguous");
    let c = v["candidates"].as_array().unwrap();
    assert_eq!(c.len(), 2);
    assert!(c.iter().any(|x| x["project"] == "/home/demo/alpha"));
    assert!(c.iter().any(|x| x["project"] == "/srv/elsewhere/Alpha"));

    let o = run(tmp.path(), &["query", "project", "alpha"]);
    assert_eq!(o.code, 2);
    assert!(o.stdout.contains("/srv/elsewhere/Alpha"), "{}", o.stdout);

    // The full path still resolves.
    ok(
        tmp.path(),
        &["query", "project", "/srv/elsewhere/Alpha", "--json"],
    );

    let o = run(
        tmp.path(),
        &["query", "project", "no-such-project", "--json"],
    );
    assert_eq!(o.code, 1);
    let v: serde_json::Value = serde_json::from_str(&o.stdout).unwrap();
    assert_eq!(v["error"], "not found");
    let o = run(tmp.path(), &["query", "project", "no-such-project"]);
    assert_eq!(o.code, 1);
    assert!(o.stderr.contains("not found"), "{}", o.stderr);
}

#[test]
fn unreadable_t3_database_gives_one_notice() {
    let tmp = tempfile::tempdir().unwrap();
    let bad = tmp.path().join("state.sqlite");
    std::fs::write(&bad, b"definitely not a sqlite database, only some text").unwrap();
    let f = fixtures();
    let o = run(
        tmp.path(),
        &[
            "scan",
            "--claude-dir",
            f.join("claude/projects").to_str().unwrap(),
            "--t3-state",
            bad.to_str().unwrap(),
        ],
    );
    assert_eq!(o.code, 0, "{}", o.stderr);
    assert_eq!(
        o.stderr.matches("notice: T3 state database").count(),
        1,
        "{}",
        o.stderr
    );
    assert!(
        o.stdout.contains("1 unreadable, 0 sessions linked"),
        "{}",
        o.stdout
    );
    assert!(
        !o.stdout.contains("enrich: 0 sessions given"),
        "{}",
        o.stdout
    );
}

#[test]
fn reindex_merges_umbrella_and_deleted_t3_worktrees_without_changing_usage() {
    let tmp = tempfile::tempdir().unwrap();
    let canonical_root = std::fs::canonicalize(tmp.path()).unwrap();
    let root = canonical_root.as_path();
    let arena = root.join("arena");
    let seat = arena.join("runs/seat-02");
    let sibling = root.join("arena-other");
    std::fs::create_dir_all(&seat).unwrap();
    std::fs::create_dir_all(&sibling).unwrap();
    let config_dir = root.join("config/usage-habit-report");
    std::fs::create_dir_all(&config_dir).unwrap();
    std::fs::write(
        config_dir.join("config.toml"),
        format!(
            "[project_aliases]\n{} = {} # umbrella\n",
            serde_json::to_string(arena.to_str().unwrap()).unwrap(),
            serde_json::to_string(arena.to_str().unwrap()).unwrap(),
        ),
    )
    .unwrap();
    // Opening through the CLI creates the schema, without scanning local logs.
    ok(root, &["--no-scan", "query", "projects", "--json"]);
    let db = Connection::open(root.join("usage.db")).unwrap();
    let t3 = root.join("state.sqlite");
    t3_state(&t3);
    db.execute_batch(
        "INSERT INTO sessions (tool, session_id, cwd, project, project_name)
        VALUES ('codex', 'deleted', '/deleted/worktree/subdir', '/deleted/worktree', 'worktree');
        INSERT INTO requests (tool, request_key, session_id, ts_ms, input_tokens, output_tokens, cost_usd)
        VALUES ('codex', 'r1', 'deleted', 1000, 123, 45, 0.25);",
    )
    .unwrap();
    let metadata = Connection::open(&t3).unwrap();
    metadata
        .execute(
            "UPDATE projection_threads SET worktree_path = '/deleted/worktree'",
            [],
        )
        .unwrap();
    for (sid, cwd) in [("seat", seat.as_path()), ("sibling", sibling.as_path())] {
        db.execute(
            "INSERT INTO sessions (tool, session_id, cwd, project, project_name)
            VALUES ('claude-code', ?1, ?2, ?2, ?1)",
            params![sid, cwd.to_str().unwrap()],
        )
        .unwrap();
    }
    let before: (i64, i64, i64, f64) = db
        .query_row(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) FROM requests",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();
    let output = ok(root, &["reindex", "--t3-state", t3.to_str().unwrap()]);
    assert!(output.contains("2 project assignments changed"), "{output}");
    let assigned = |sid: &str| -> String {
        db.query_row(
            "SELECT project FROM sessions WHERE session_id = ?1",
            [sid],
            |r| r.get(0),
        )
        .unwrap()
    };
    assert_eq!(
        assigned("seat"),
        std::fs::canonicalize(&arena).unwrap().to_str().unwrap()
    );
    assert_eq!(assigned("sibling"), sibling.to_str().unwrap());
    assert_eq!(assigned("deleted"), "/home/demo/gamma");
    let after = db
        .query_row(
            "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd) FROM requests",
            [],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, f64>(3)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(before, after);
    let detail: serde_json::Value = serde_json::from_str(&ok(
        root,
        &[
            "--no-scan",
            "query",
            "project",
            seat.to_str().unwrap(),
            "--json",
        ],
    ))
    .unwrap();
    assert_eq!(detail["project"], assigned("seat"));
    assert!(
        ok(root, &["reindex", "--t3-state", t3.to_str().unwrap()])
            .contains("0 project assignments changed")
    );
}
