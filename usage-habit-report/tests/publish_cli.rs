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
    store.conn.execute_batch("INSERT INTO sessions (tool, session_id, cwd, project, title, display_title, t3_title, label) VALUES ('codex', 'private-session-id', '/home/demo/private/work', '/home/demo/private', 'Private <opening> title', 'Private display headline', 'Private T3 headline', 'coding'); INSERT INTO requests (tool, request_key, session_id, ts_ms, canonical_model, input_tokens) VALUES ('codex', 'request', 'private-session-id', 1792380600000, 'demo-model', 42);").unwrap();
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
            .contains("17 files written; committed; not pushed (disabled)")
    );
    assert!(!out.join("stale.html").exists());
    assert_eq!(std::fs::read_dir(&out).unwrap().count(), 17);
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
        // Model names survive redaction and reach the public page through the
        // model matrix, so this deliberately plants a private value there.
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
            usage_habit_report::privacy::check(
                &store.conn,
                &out,
                Some(Path::new("/home/demo")),
                &Default::default(),
                &Default::default()
            )
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
    usage_habit_report::privacy::check(
        &store.conn,
        &out,
        None,
        &Default::default(),
        &Default::default(),
    )
    .unwrap();
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
                assert!(err.starts_with("publish: 17 files"));
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

#[test]
fn allowlisted_project_publishes_with_config_override_and_no_push() {
    let tmp = tempfile::tempdir().unwrap();
    let store = fixture(&tmp.path().join("usage.db"));
    store
        .conn
        .execute("UPDATE sessions SET project_name = 'public-repo'", [])
        .unwrap();
    let root = tmp.path().join("repo");
    repo(&root);
    let out = root.join("site");
    let config = tmp.path().join("override.toml");
    std::fs::write(&config, format!("[sources]\ncodex_state_dir = '/home/demo/unused'\n[publish]\ndir = '{}'\npush = true\n[public_projects]\n\"/home/demo/private\" = \"https://github.com/private/public-repo\"\n", out.display())).unwrap();
    let result = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
        .arg("--db")
        .arg(tmp.path().join("usage.db"))
        .args(["--no-scan", "--offline", "publish", "--no-push"])
        .env("HOME", tmp.path().join("home"))
        .env("XDG_CONFIG_HOME", tmp.path().join("config"))
        .env("USAGE_HABIT_REPORT_CONFIG", &config)
        .output()
        .unwrap();
    assert!(result.status.success(), "allowlisted publish failed");
    assert!(String::from_utf8_lossy(&result.stdout).contains("committed; not pushed (disabled)"));
    let body = std::fs::read_to_string(out.join("index-all.html")).unwrap();
    assert!(body.contains(
        "<a href=\"https://github.com/private/public-repo\" rel=\"noopener\">public-repo</a>"
    ));
    assert!(!body.contains("/home/demo/private"));
    assert_eq!(git(&root, &["log", "-1", "--format=%s"]), publish::MESSAGE);
}

#[test]
fn public_allowlist_exempts_words_and_urls_but_never_paths_or_ids() {
    let tmp = tempfile::tempdir().unwrap();
    let store = fixture(&tmp.path().join("usage.db"));
    store
        .conn
        .execute("UPDATE sessions SET project_name = 'public-repo'", [])
        .unwrap();
    let out = tmp.path().join("pages");
    std::fs::create_dir(&out).unwrap();
    let file = out.join("index.html");
    let public = std::collections::BTreeMap::from([(
        "/home/demo/private".to_owned(),
        "https://github.com/private/public-repo".to_owned(),
    )]);
    let safe = "<a href=\"https://github.com/private/public-repo\">public-repo</a>";
    std::fs::write(&file, safe).unwrap();
    usage_habit_report::privacy::check(&store.conn, &out, None, &public, &Default::default())
        .unwrap();
    for leaked in [
        "private",
        "https://github.com/demo/unapproved",
        "<style>/home/demo/private</style>",
        "<script>private-session-id</script>",
    ] {
        std::fs::write(&file, format!("{safe}{leaked}")).unwrap();
        assert!(
            usage_habit_report::privacy::check(
                &store.conn,
                &out,
                None,
                &public,
                &Default::default()
            )
            .is_err()
        );
    }
}

#[test]
fn publish_accepts_project_folders_that_collide_with_template_words() {
    let tmp = tempfile::tempdir().unwrap();
    let store = fixture(&tmp.path().join("usage.db"));
    store.conn.execute_batch("UPDATE sessions SET project = '/home/demo/project', project_name = 'project'; INSERT INTO sessions (tool, session_id, project, project_name) VALUES ('codex', 'second-fixture-id', '/home/demo/sessions', 'sessions');").unwrap();
    let root = tmp.path().join("repo");
    repo(&root);
    let out = root.join("site");
    let result = cli(
        tmp.path(),
        &["publish", "--no-push", "--out", out.to_str().unwrap()],
    );
    assert!(result.status.success(), "template-word publish failed");
    assert_eq!(std::fs::read_dir(out).unwrap().count(), 17);
}

/// A clone of a bare origin whose default branch is `main`, checked out on a
/// project branch that tracks `origin/project`, like the design repository.
fn project_branch_clone(tmp: &Path) -> (std::path::PathBuf, std::path::PathBuf) {
    let seed = tmp.join("seed");
    repo(&seed);
    let origin = tmp.join("origin.git");
    git(
        tmp,
        &[
            "clone",
            "--bare",
            seed.to_str().unwrap(),
            origin.to_str().unwrap(),
        ],
    );
    let root = tmp.join("repo");
    git(
        tmp,
        &["clone", origin.to_str().unwrap(), root.to_str().unwrap()],
    );
    git(&root, &["config", "user.name", "Demo"]);
    git(&root, &["config", "user.email", "demo@example.invalid"]);
    git(&root, &["switch", "-c", "project"]);
    git(&root, &["push", "-u", "origin", "project"]);
    (root, origin)
}

fn serve_first_line_and_stderr(tmp: &Path, out: &Path) -> String {
    use std::io::{BufRead, BufReader, Read};
    use std::process::Stdio;
    let config = tmp.join("config/usage-habit-report");
    std::fs::create_dir_all(&config).unwrap();
    std::fs::write(
        config.join("config.toml"),
        format!("[publish]\ndir = '{}'\n", out.display()),
    )
    .unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
        .arg("--db")
        .arg(tmp.join("usage.db"))
        .args([
            "--no-scan",
            "--offline",
            "serve",
            "--no-open",
            "--port",
            "0",
        ])
        .env("HOME", tmp.join("home"))
        .env("XDG_CONFIG_HOME", tmp.join("config"))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut first = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut first)
        .unwrap();
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(first.starts_with("serving http://127.0.0.1:"));
    let mut err = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut err)
        .unwrap();
    err
}

#[test]
fn serve_on_a_project_branch_updates_the_deployed_default_branch() {
    let tmp = tempfile::tempdir().unwrap();
    fixture(&tmp.path().join("usage.db"));
    let (root, origin) = project_branch_clone(tmp.path());
    let err = serve_first_line_and_stderr(tmp.path(), &root.join("site"));
    assert!(
        err.contains("committed; pushed; main fast-forwarded"),
        "{err}"
    );
    let head = git(&root, &["rev-parse", "HEAD"]);
    assert_eq!(git(&root, &["log", "-1", "--format=%s"]), publish::MESSAGE);
    assert_eq!(git(&origin, &["rev-parse", "project"]), head);
    // GitHub Pages deploys from the default branch.
    assert_eq!(git(&origin, &["rev-parse", "main"]), head);
}

#[test]
fn default_branch_moves_only_past_snapshot_commits() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    let store = fixture(&db);
    let (root, origin) = project_branch_clone(tmp.path());
    let out = root.join("site");
    let main = git(&origin, &["rev-parse", "main"]);

    // Disabled pushing leaves both remote branches alone.
    let summary = publish::run(&db, &out, false, NOW).unwrap();
    assert!(
        summary.ends_with("committed; not pushed (disabled)"),
        "{summary}"
    );
    assert_eq!(git(&origin, &["rev-parse", "main"]), main);

    // Pushed but unmerged code keeps the public pages on the default branch.
    git(
        &root,
        &["commit", "--allow-empty", "-m", "Code awaiting review"],
    );
    git(&root, &["push"]);
    store
        .conn
        .execute("UPDATE requests SET input_tokens = 43", [])
        .unwrap();
    let summary = publish::run(&db, &out, true, NOW).unwrap();
    assert!(
        summary.ends_with("pushed; main not updated (1 other commits awaiting merge; merge project into main to publish)"),
        "{summary}"
    );
    assert_eq!(git(&origin, &["rev-parse", "main"]), main);
    assert_eq!(
        git(&origin, &["rev-parse", "project"]),
        git(&root, &["rev-parse", "HEAD"])
    );

    // A default branch that moved on is never rewritten.
    let (root, origin) = {
        let tmp = tmp.path().join("diverged");
        std::fs::create_dir(&tmp).unwrap();
        let (root, origin) = project_branch_clone(&tmp);
        let other = tmp.join("other");
        git(
            &tmp,
            &["clone", origin.to_str().unwrap(), other.to_str().unwrap()],
        );
        git(&other, &["config", "user.name", "Demo"]);
        git(&other, &["config", "user.email", "demo@example.invalid"]);
        git(
            &other,
            &["commit", "--allow-empty", "-m", "Merged elsewhere"],
        );
        git(&other, &["push"]);
        (root, origin)
    };
    let main = git(&origin, &["rev-parse", "main"]);
    let summary = publish::run(&db, &root.join("site"), true, NOW).unwrap();
    assert!(
        summary.ends_with("pushed; main not updated (it has commits this branch lacks)"),
        "{summary}"
    );
    assert_eq!(git(&origin, &["rev-parse", "main"]), main);
}

#[test]
fn publish_on_the_default_branch_pushes_once() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    fixture(&db);
    let (root, origin) = project_branch_clone(tmp.path());
    git(&root, &["switch", "main"]);
    let summary = publish::run(&db, &root.join("site"), true, NOW).unwrap();
    assert!(summary.ends_with("committed; pushed"), "{summary}");
    assert_eq!(
        git(&origin, &["rev-parse", "main"]),
        git(&root, &["rev-parse", "HEAD"])
    );
    // A later run with nothing new still reports the default branch state.
    git(&root, &["switch", "project"]);
    git(&root, &["merge", "--ff-only", "main"]);
    git(&root, &["push"]);
    let summary = publish::run(&db, &root.join("site"), true, NOW).unwrap();
    assert!(
        summary.ends_with("no commit; pushed; main up to date"),
        "{summary}"
    );
}

#[test]
fn snapshot_message_alone_does_not_ship_other_files() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    fixture(&db);
    let (root, origin) = project_branch_clone(tmp.path());
    let out = root.join("site");
    let main = git(&origin, &["rev-parse", "main"]);

    // An amended snapshot commit that also adds code is not pushed.
    publish::run(&db, &out, false, NOW).unwrap();
    std::fs::write(root.join("unreviewed-code.js"), "ship()").unwrap();
    git(&root, &["add", "unreviewed-code.js"]);
    git(&root, &["commit", "--amend", "--no-edit"]);
    let summary = publish::run(&db, &out, true, NOW).unwrap();
    assert!(
        summary.ends_with("no commit; not pushed (1 other unpushed commits)"),
        "{summary}"
    );
    assert_eq!(git(&origin, &["rev-parse", "main"]), main);

    // Pushed by hand, it still never reaches the default branch.
    git(&root, &["push"]);
    let summary = publish::run(&db, &out, true, NOW).unwrap();
    assert!(
        summary.ends_with(
            "no commit; pushed; main not updated (1 other commits awaiting merge; merge project into main to publish)"
        ),
        "{summary}"
    );
    assert_eq!(git(&origin, &["rev-parse", "main"]), main);

    // A later snapshot-message commit that deletes the code leaves no net
    // change outside the snapshot, but the commit that added it still counts.
    git(&root, &["rm", "--quiet", "unreviewed-code.js"]);
    git(&root, &["commit", "-m", publish::MESSAGE]);
    git(&root, &["push"]);
    let summary = publish::run(&db, &out, true, NOW).unwrap();
    assert!(
        summary.ends_with(
            "main not updated (2 other commits awaiting merge; merge project into main to publish)"
        ),
        "{summary}"
    );
    assert_eq!(git(&origin, &["rev-parse", "main"]), main);
}

#[test]
fn unknown_remote_default_branch_is_reported() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    fixture(&db);
    let (root, origin) = project_branch_clone(tmp.path());
    let main = git(&origin, &["rev-parse", "main"]);
    git(&root, &["remote", "set-head", "origin", "--delete"]);
    let summary = publish::run(&db, &root.join("site"), true, NOW).unwrap();
    assert!(
        summary.ends_with(
            "committed; pushed; default branch not checked (origin/HEAD unknown; run `git remote set-head origin --auto`)"
        ),
        "{summary}"
    );
    assert_eq!(git(&origin, &["rev-parse", "main"]), main);
}
