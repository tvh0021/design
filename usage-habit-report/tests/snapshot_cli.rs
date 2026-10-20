//! Public export privacy checks never print private values, even on failure.
use rusqlite::{Connection, OpenFlags};
use std::{collections::BTreeSet, path::Path, process::Command};
use usage_habit_report::{
    privacy::{body_text, contains_whole_word, word_candidate_matches},
    serve::html,
    store::Store,
};

fn export(db: &Path, out: &Path, window: Option<&str>) -> std::process::Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"));
    cmd.arg("--db")
        .arg(db)
        .arg("snapshot")
        .arg("--out")
        .arg(out)
        .env("XDG_CONFIG_HOME", db.parent().unwrap().join("config"));
    if let Some(w) = window {
        cmd.args(["--window", w]);
    }
    let output = cmd.output().unwrap();
    // Error messages may contain paths. Report the exit code only.
    assert!(
        output.status.success(),
        "snapshot failed: {:?}",
        output.status.code()
    );
    output
}

fn page_vocabulary(out: &Path) -> BTreeSet<String> {
    std::fs::read_dir(out)
        .unwrap()
        .flat_map(|entry| {
            body_text(&std::fs::read_to_string(entry.unwrap().path()).unwrap())
                .split(|c: char| !c.is_ascii_alphanumeric() && c != '_')
                .filter(|word| !word.is_empty())
                .map(str::to_owned)
                .collect::<Vec<_>>()
        })
        .collect()
}

#[test]
fn word_privacy_rules_preserve_boundaries_case_and_escaped_forms() {
    let vocabulary = BTreeSet::from(["project".to_owned()]);
    let text = body_text(
        "<style>asset-only</style><body>project secret secret_suffix prefix_secret Secret a&amp;b xy private phrase xa-a-a</body><script>script-only</script>",
    );
    for candidate in ["asset-only", "script-only", "project", "xy", "SECRET"] {
        assert!(!word_candidate_matches(&text, candidate, &vocabulary));
    }
    for candidate in ["secret", "Secret", "a&amp;b", "private phrase", "a-a"] {
        assert!(word_candidate_matches(&text, candidate, &vocabulary));
    }
    for text in ["secret_suffix", "prefix_secret", "2secret", "secret2"] {
        assert!(!contains_whole_word(text, "secret"));
    }
    assert!(contains_whole_word("ésecreté", "secret"));
    assert!(!word_candidate_matches(&text, "a&b", &vocabulary));
}

fn assert_redacted(conn: &Connection, out: &Path, vocabulary: &BTreeSet<String>) {
    usage_habit_report::privacy::check(conn, out, None).unwrap();
    let files: Vec<_> = std::fs::read_dir(out)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(files.len(), 16);
    for file in &files {
        let body = std::fs::read_to_string(file).unwrap();
        // All resources are inline; the only navigation is to generated files.
        for forbidden in [
            "http://",
            "https://",
            "src=",
            "<link",
            "@import",
            "url(",
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
        ] {
            assert!(
                !body.contains(forbidden),
                "external resource mechanism found"
            );
        }
        for link in body.split("href=\"").skip(1) {
            let target = link.split('"').next().unwrap();
            assert!(out.join(target).is_file(), "static link has no output file");
        }
        assert!(body.contains(html::CSS));
        assert!(body.contains(html::JS));
    }
    for result in usage_habit_report::privacy::audit(conn, out, vocabulary).unwrap() {
        eprintln!(
            "redaction {}: {} candidates, {} skipped short, {} fixture vocabulary, {} matches",
            result.category,
            result.candidates,
            result.skipped_short,
            result.vocabulary_words,
            result.matches
        );
        assert_eq!(
            result.matches, 0,
            "redaction {} failed; counts only, no private strings",
            result.category
        );
    }
}

fn fixture_store(db: &Path) -> Store {
    let store = Store::open(db).unwrap();
    store.conn.execute_batch(
        "INSERT INTO sessions (tool, session_id, parent_session_id, is_subagent, title, cwd, project, project_name, t3_thread_id, t3_title, started_at_ms, ended_at_ms, label) VALUES
        ('codex', 'fixture-session-private-id', NULL, 0, 'Fixture private <title> & opening prompt', '/home/demo/private-orchard/work', '/home/demo/private-orchard', 'private-orchard', 'fixture-t3-private-id', 'Fixture private T3 title', 1792380600000, 1792384200000, 'coding'),
        ('codex', 'fixture-child-private-id', 'fixture-session-private-id', 1, 'Fixture child private title', '/home/demo/private-violet', '/home/demo/private-violet', 'private-violet', 'fixture-child-t3-private-id', 'Fixture child T3 private title', 1792380601000, 1792384200000, NULL);
        INSERT INTO requests (tool, request_key, session_id, is_subagent, ts_ms, canonical_model, input_tokens, output_tokens, cost_usd) VALUES
        ('codex', 'fixture-private-request-key', 'fixture-session-private-id', 0, 1792380600000, 'synthetic-model', 100, 50, 1.49),
        ('codex', 'fixture-child-request-key', 'fixture-child-private-id', 1, 1792380601000, 'synthetic-model', 50, 20, 0.49);"
    ).unwrap();
    store.conn.execute_batch("UPDATE sessions SET display_title = 'Invented private headline <demo> & example', title_source = 'codex-name'").unwrap();
    store
}

#[test]
fn fixture_snapshot_is_redacted_offline_and_linked() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    let store = fixture_store(&db);
    let out = tmp.path().join("out");
    export(&db, &out, None);
    // The fixture must not exempt its own private values as page vocabulary.
    assert_redacted(&store.conn, &out, &BTreeSet::new());
    for entry in std::fs::read_dir(&out).unwrap() {
        let body = std::fs::read_to_string(entry.unwrap().path()).unwrap();
        assert!(!body.contains("fixture-private-request-key"));
        for panel in [
            "activity", "totals", "models", "agents", "projects", "sessions", "cache", "spend",
        ] {
            assert!(body.contains(&format!("<section id=\"{panel}\"")));
        }
    }
    // Snapshot opens the store read-only and performs no scan, pricing, or migration.
    assert_eq!(
        store
            .conn
            .query_row("SELECT SUM(cost_usd) FROM requests", [], |r| r
                .get::<_, f64>(0))
            .unwrap(),
        1.98
    );
}

#[test]
fn explicit_windows_write_index_and_only_link_to_their_four_files() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    Store::open(&db).unwrap();
    for window in ["4w", "12w", "all"] {
        let out = tmp.path().join(window);
        export(&db, &out, Some(window));
        assert_eq!(std::fs::read_dir(&out).unwrap().count(), 4);
        let body = std::fs::read_to_string(out.join("index.html")).unwrap();
        for link in body.split("href=\"").skip(1) {
            assert!(out.join(link.split('"').next().unwrap()).is_file());
        }
        assert!(body.contains("De-identified export. Generated "));
        assert!(!body.contains("usage.db"));
    }
}

#[test]
fn invalid_window_and_missing_store_write_nothing() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("missing.db");
    let out = tmp.path().join("out");
    for args in [vec!["--window", "nope"], vec![]] {
        let output = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
            .arg("--db")
            .arg(&db)
            .env("XDG_CONFIG_HOME", tmp.path().join("config"))
            .arg("snapshot")
            .arg("--out")
            .arg(&out)
            .args(args)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(!db.exists());
        assert!(!out.exists());
    }
}

#[test]
fn real_store_snapshot_is_redacted() {
    if std::env::var("USAGE_HABIT_REPORT_REAL_STORE_TESTS").as_deref() != Ok("1") {
        eprintln!("skipped: set USAGE_HABIT_REPORT_REAL_STORE_TESTS=1 to check the real store");
        return;
    }
    let Some(home) = std::env::var_os("HOME") else {
        eprintln!("skipped: no home directory");
        return;
    };
    let db = std::env::var_os("USAGE_HABIT_REPORT_REAL_STORE_DB")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| Path::new(&home).join(".local/share/usage-habit-report/usage.db"));
    if !db.is_file() {
        eprintln!("skipped: real store is absent");
        return;
    }
    let conn = Connection::open_with_flags(&db, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let tmp = tempfile::tempdir().unwrap();
    let output = export(&db, tmp.path(), None);
    // CLI stdout contains counts and duration only.
    eprintln!("{}", String::from_utf8_lossy(&output.stdout).trim());
    let mut sizes: Vec<_> = std::fs::read_dir(tmp.path())
        .unwrap()
        .map(|e| {
            let e = e.unwrap();
            (e.file_name(), e.metadata().unwrap().len())
        })
        .collect();
    sizes.sort();
    for (name, bytes) in sizes {
        eprintln!("{}: {bytes} bytes", name.to_string_lossy());
    }
    let fixture = tempfile::tempdir().unwrap();
    let fixture_db = fixture.path().join("fixture.db");
    let _fixture_store = fixture_store(&fixture_db);
    let fixture_out = fixture.path().join("out");
    export(&fixture_db, &fixture_out, None);
    assert_redacted(&conn, tmp.path(), &page_vocabulary(&fixture_out));
}
