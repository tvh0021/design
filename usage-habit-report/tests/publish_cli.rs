use std::{
    path::Path,
    process::{Command, Output},
};
use usage_habit_report::{publish, store::Store};

const NOW: i64 = 1792384200000;

fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "fixture git failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().to_owned()
}
fn repo(dir: &Path) {
    std::fs::create_dir_all(dir).unwrap();
    git(dir, &["init", "-b", "main"]);
    git(dir, &["config", "user.name", "Demo"]);
    git(dir, &["config", "user.email", "demo@example.invalid"]);
    git(dir, &["commit", "--allow-empty", "-m", "Initial fixture"]);
}
fn fixture(db: &Path) -> Store {
    let store = Store::open(db).unwrap();
    store.conn.execute_batch("INSERT INTO sessions (tool, session_id, cwd, project, title, display_title, t3_title) VALUES ('codex', 'private-session-id', '/home/demo/private/work', '/home/demo/private', 'Private <opening> title', 'Private display headline', 'Private T3 headline'); INSERT INTO requests (tool, request_key, session_id, ts_ms, canonical_model, input_tokens) VALUES ('codex', 'request', 'private-session-id', 1792380600000, 'demo-model', 42);").unwrap();
    store
}
fn cli(tmp: &Path, args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
        .arg("--db")
        .arg(tmp.join("usage.db"))
        .args(["--no-scan", "--offline"])
        .args(args)
        .env("HOME", tmp.join("home"))
        .env("XDG_CONFIG_HOME", tmp.join("config"))
        .output()
        .unwrap()
}

#[test]
fn fixture_publish_commits_once_removes_stale_files_and_preserves_unrelated_work() {
    let tmp = tempfile::tempdir().unwrap();
    fixture(&tmp.path().join("usage.db"));
    let root = tmp.path().join("repo");
    repo(&root);
    let out = root.join("site");
    std::fs::create_dir(&out).unwrap();
    std::fs::write(out.join("stale.html"), "old").unwrap();
    git(&root, &["add", "site"]);
    git(&root, &["commit", "-m", "Old export"]);
    std::fs::write(root.join("unrelated"), "leave alone").unwrap();
    let args = ["publish", "--no-push", "--out", out.to_str().unwrap()];
    let first = cli(tmp.path(), &args);
    assert!(first.status.success(), "publish failed");
    assert!(
        String::from_utf8_lossy(&first.stdout)
            .contains("16 files written; committed; not pushed (disabled)")
    );
    assert!(!out.join("stale.html").exists());
    assert_eq!(std::fs::read_dir(&out).unwrap().count(), 16);
    assert_eq!(git(&root, &["log", "-1", "--format=%s"]), publish::MESSAGE);
    let head = git(&root, &["rev-parse", "HEAD"]);
    let second = cli(tmp.path(), &args);
    assert!(second.status.success());
    assert!(String::from_utf8_lossy(&second.stdout).contains("no commit"));
    assert_eq!(head, git(&root, &["rev-parse", "HEAD"]));
    assert_eq!(git(&root, &["status", "--porcelain"]), "?? unrelated");
}

#[test]
fn privacy_failure_preserves_directory_and_index_for_paths_and_titles() {
    for value in [
        "/home/demo/private",
        "Private <opening> title",
        "Private display headline",
        "private-session-id",
    ] {
        let tmp = tempfile::tempdir().unwrap();
        let store = fixture(&tmp.path().join("usage.db"));
        // Model names survive redaction, so this deliberately plants a private value.
        store
            .conn
            .execute("UPDATE requests SET canonical_model = ?1", [value])
            .unwrap();
        let root = tmp.path().join("repo");
        repo(&root);
        let out = root.join("site");
        std::fs::create_dir(&out).unwrap();
        std::fs::write(out.join("previous"), "keep").unwrap();
        let head = git(&root, &["rev-parse", "HEAD"]);
        let result = cli(
            tmp.path(),
            &["publish", "--no-push", "--out", out.to_str().unwrap()],
        );
        assert!(!result.status.success());
        assert!(String::from_utf8_lossy(&result.stderr).contains("privacy guard failed"));
        assert_eq!(
            std::fs::read_to_string(out.join("previous")).unwrap(),
            "keep"
        );
        assert_eq!(std::fs::read_dir(&out).unwrap().count(), 1);
        assert_eq!(git(&root, &["rev-parse", "HEAD"]), head);
        assert!(git(&root, &["diff", "--cached", "--name-only"]).is_empty());
    }
}

#[test]
fn other_unpushed_commit_prevents_push_to_bare_origin() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    fixture(&db);
    let root = tmp.path().join("repo");
    repo(&root);
    let origin = tmp.path().join("origin.git");
    git(
        tmp.path(),
        &[
            "clone",
            "--bare",
            root.to_str().unwrap(),
            origin.to_str().unwrap(),
        ],
    );
    git(
        &root,
        &["remote", "add", "origin", origin.to_str().unwrap()],
    );
    git(&root, &["fetch", "origin"]);
    git(&root, &["branch", "--set-upstream-to=origin/main"]);
    let remote_head = git(&origin, &["rev-parse", "main"]);
    git(&root, &["commit", "--allow-empty", "-m", "Other work"]);
    let summary = publish::run(&db, &root.join("site"), true, NOW).unwrap();
    assert!(summary.contains("committed; not pushed (1 other unpushed commits)"));
    assert_eq!(git(&origin, &["rev-parse", "main"]), remote_head);
    assert_eq!(git(&root, &["log", "-1", "--format=%s"]), publish::MESSAGE);
}

#[test]
fn staged_work_is_not_committed() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    fixture(&db);
    let root = tmp.path().join("repo");
    repo(&root);
    std::fs::write(root.join("other"), "staged").unwrap();
    git(&root, &["add", "other"]);
    assert!(publish::run(&db, &root.join("site"), false, NOW).is_err());
    assert_eq!(git(&root, &["diff", "--cached", "--name-only"]), "other");
    assert_eq!(git(&root, &["log", "-1", "--format=%s"]), "Initial fixture");
}

#[test]
fn configuration_and_out_override_disable_push() {
    let tmp = tempfile::tempdir().unwrap();
    fixture(&tmp.path().join("usage.db"));
    let root = tmp.path().join("repo");
    repo(&root);
    let config = tmp.path().join("config/usage-habit-report");
    std::fs::create_dir_all(&config).unwrap();
    std::fs::write(
        config.join("config.toml"),
        format!(
            "[publish]\ndir = '{}' # destination\npush = false\n",
            root.join("site").display()
        ),
    )
    .unwrap();
    let result = cli(tmp.path(), &["publish"]);
    assert!(result.status.success());
    assert!(String::from_utf8_lossy(&result.stdout).contains("not pushed (disabled)"));
    let override_dir = root.join("override");
    assert!(
        cli(
            tmp.path(),
            &["publish", "--out", override_dir.to_str().unwrap()]
        )
        .status
        .success()
    );
    assert!(override_dir.join("index.html").exists());
}

#[test]
fn config_parser_rejects_invalid_scalars_and_preserves_source_paths() {
    let config = usage_habit_report::config::parse("[sources]\ncodex_state_dir = '/home/demo/.codex'\n[publish]\ndir = \"/home/demo/public\\tfiles\" # comment\npush = true\n").unwrap();
    assert_eq!(
        config.sources["codex_state_dir"],
        Path::new("/home/demo/.codex")
    );
    assert_eq!(
        config.publish_dir.unwrap(),
        Path::new("/home/demo/public\tfiles")
    );
    assert_eq!(config.publish_push, Some(true));
    for text in [
        "[publish]\npush = 'true'",
        "[publish]\ndir = false",
        "[publish]\nunknown = 'x'",
        "[publish]\ndir = 'x' junk",
    ] {
        assert!(usage_habit_report::config::parse(text).is_err());
    }
}

#[test]
fn privacy_checks_raw_and_escaped_values_assets_short_titles_and_home() {
    let tmp = tempfile::tempdir().unwrap();
    let store = fixture(&tmp.path().join("usage.db"));
    let out = tmp.path().join("pages");
    std::fs::create_dir(&out).unwrap();
    let file = out.join("index.html");
    for value in [
        "Private <opening> title",
        "Private &lt;opening&gt; title",
        "Private T3 headline",
        "Private display headline",
        "private-session-id",
        "/home/demo/private/work",
        "/home/demo/private",
        "/home/demo",
    ] {
        std::fs::write(&file, format!("<script>{value}</script>")).unwrap();
        assert!(
            usage_habit_report::privacy::check(&store.conn, &out, Some(Path::new("/home/demo")))
                .is_err()
        );
    }
    store
        .conn
        .execute(
            "UPDATE sessions SET title = 'short title', display_title = 'tiny', t3_title = 'small'",
            [],
        )
        .unwrap();
    std::fs::write(&file, "short title tiny small").unwrap();
    usage_habit_report::privacy::check(&store.conn, &out, None).unwrap();
}

#[test]
fn serve_skips_publish_or_warns_and_still_starts() {
    use std::io::{BufRead, BufReader, Read};
    use std::process::Stdio;
    for mode in ["skip", "success", "privacy", "git"] {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("usage.db");
        let store = fixture(&db);
        if mode == "privacy" {
            store
                .conn
                .execute(
                    "UPDATE requests SET canonical_model = '/home/demo/private'",
                    [],
                )
                .unwrap();
        }
        let root = tmp.path().join("repo");
        repo(&root);
        if mode == "git" {
            std::fs::write(root.join("other"), "staged").unwrap();
            git(&root, &["add", "other"]);
        }
        let out = root.join("site");
        std::fs::create_dir(&out).unwrap();
        std::fs::write(out.join("old"), "keep").unwrap();
        let config = tmp.path().join("config/usage-habit-report");
        std::fs::create_dir_all(&config).unwrap();
        std::fs::write(
            config.join("config.toml"),
            format!("[publish]\ndir = '{}'\npush = false\n", out.display()),
        )
        .unwrap();
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"));
        cmd.arg("--db")
            .arg(&db)
            .args([
                "--no-scan",
                "--offline",
                "serve",
                "--no-open",
                "--port",
                "0",
            ])
            .env("HOME", tmp.path().join("home"))
            .env("XDG_CONFIG_HOME", tmp.path().join("config"))
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if mode == "skip" {
            cmd.arg("--no-publish");
        }
        let mut child = cmd.spawn().unwrap();
        let mut first = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut first)
            .unwrap();
        child.kill().unwrap();
        child.wait().unwrap();
        let mut err = String::new();
        child
            .stderr
            .take()
            .unwrap()
            .read_to_string(&mut err)
            .unwrap();
        assert!(first.starts_with("serving http://127.0.0.1:"));
        match mode {
            "skip" => {
                assert!(err.is_empty());
                assert!(out.join("old").exists());
            }
            "privacy" => {
                assert!(err.contains("warning: publish failed: privacy guard failed"));
                assert!(out.join("old").exists());
            }
            "git" => {
                assert!(err.contains("warning: publish failed: git index"));
            }
            _ => {
                assert!(out.join("index.html").exists());
                assert!(err.starts_with("publish: 16 files"));
            }
        }
        assert!(
            store
                .conn
                .query_row("SELECT SUM(cost_usd) IS NULL FROM requests", [], |r| r
                    .get::<_, bool>(0))
                .unwrap()
        );
    }
}

#[test]
fn publish_catches_up_and_no_scan_preserves_store() {
    let tmp = tempfile::tempdir().unwrap();
    let home = tmp.path().join("home");
    let logs = home.join(".codex/sessions");
    std::fs::create_dir_all(&logs).unwrap();
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/codex");
    for entry in walkdir::WalkDir::new(fixtures) {
        let entry = entry.unwrap();
        if entry.file_type().is_file()
            && entry.file_name().to_string_lossy().starts_with("rollout-")
        {
            std::fs::copy(entry.path(), logs.join(entry.file_name())).unwrap();
        }
    }
    let db = tmp.path().join("usage.db");
    let store = Store::open(&db).unwrap();
    let out = tmp.path().join("out");
    assert!(
        cli(
            tmp.path(),
            &["publish", "--no-push", "--out", out.to_str().unwrap()]
        )
        .status
        .success()
    );
    assert_eq!(
        store
            .conn
            .query_row("SELECT COUNT(*) FROM requests", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
    // The ingest fixtures deliberately use unpriced demo models. A known-model
    // row verifies that publish also runs the usual pricing step.
    store.conn.execute_batch("INSERT INTO sessions (tool, session_id) VALUES ('codex', 'pricing-fixture-id'); INSERT INTO requests (tool, request_key, session_id, ts_ms, model, input_tokens) VALUES ('codex', 'price-fixture', 'pricing-fixture-id', 1792380600000, 'claude-opus-5-5', 100);").unwrap();
    let result = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
        .arg("--db")
        .arg(db)
        .args(["--offline", "publish", "--no-push", "--out"])
        .arg(out)
        .env("HOME", &home)
        .env("CODEX_HOME", home.join(".codex"))
        .env("XDG_CONFIG_HOME", tmp.path().join("config"))
        .output()
        .unwrap();
    assert!(result.status.success(), "catch-up publish failed");
    assert!(
        store
            .conn
            .query_row("SELECT COUNT(*) FROM requests", [], |r| r.get::<_, i64>(0))
            .unwrap()
            > 0
    );
    assert!(
        store
            .conn
            .query_row(
                "SELECT COUNT(*) FROM requests WHERE cost_usd IS NOT NULL",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap()
            > 0
    );
}

#[test]
fn unchanged_data_across_a_clock_tick_makes_no_commit() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    fixture(&db);
    let root = tmp.path().join("repo");
    repo(&root);
    let out = root.join("site");
    publish::run(&db, &out, false, NOW).unwrap();
    let head = git(&root, &["rev-parse", "HEAD"]);
    let summary = publish::run(&db, &out, false, NOW + 60_000).unwrap();
    assert!(summary.contains("no commit"));
    assert_eq!(git(&root, &["rev-parse", "HEAD"]), head);
}
