//! Public export privacy checks never print private values, even on failure.
use rusqlite::{Connection, OpenFlags};
use std::{collections::BTreeSet, path::Path, process::Command};
use usage_habit_report::{
    privacy::{body_text, contains_whole_word, word_candidate_matches},
    serve::{html, snapshot},
    store::Store,
};

fn export(db: &Path, out: &Path, window: Option<&str>) -> std::process::Output {
    export_with_config(db, out, window, None)
}

fn export_with_config(
    db: &Path,
    out: &Path,
    window: Option<&str>,
    config: Option<&Path>,
) -> std::process::Output {
    let empty = tempfile::NamedTempFile::new().unwrap();
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"));
    cmd.env("USAGE_HABIT_REPORT_CONFIG", config.unwrap_or(empty.path()));
    cmd.arg("--db")
        .arg(db)
        .arg("snapshot")
        .arg("--out")
        .arg(out)
        .env("XDG_CONFIG_HOME", db.parent().unwrap().join("config"));
    if let Some(w) = window {
        cmd.args(["--window", w]);
    }
    cmd.arg("--no-scan");
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
    assert_redacted_with_allowlist(conn, out, vocabulary, &Default::default());
}

fn assert_redacted_with_allowlist(
    conn: &Connection,
    out: &Path,
    vocabulary: &BTreeSet<String>,
    allowlist: &std::collections::BTreeMap<String, String>,
) {
    usage_habit_report::privacy::check(conn, out, None, allowlist, vocabulary).unwrap();
    let files: Vec<_> = std::fs::read_dir(out)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(files.len(), 17);
    for file in &files {
        let body = std::fs::read_to_string(file).unwrap();

        for forbidden in [
            "http://",
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
            assert!(
                allowlist.values().any(|url| html::esc(url) == target)
                    || out.join(target).is_file(),
                "link has no output file or allowlist entry"
            );
        }
        assert!(body.contains(html::CSS));
        // The value page has no activity map, so it carries no script.
        let is_value = file.file_name().unwrap() == snapshot::VALUE_FILENAME;
        assert_eq!(body.contains(html::JS), !is_value);
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
        let path = entry.unwrap().path();
        let body = std::fs::read_to_string(&path).unwrap();
        assert!(!body.contains("fixture-private-request-key"));
        if path.file_name().unwrap() == snapshot::VALUE_FILENAME {
            assert!(body.contains("<section id=\"value\""));
            continue;
        }
        for panel in [
            "activity", "models", "agents", "projects", "spend", "limits",
        ] {
            assert!(body.contains(&format!("<section id=\"{panel}\"")));
        }
        // Public pages fold these three into the spend section.
        for panel in ["totals", "sessions", "cache"] {
            assert!(!body.contains(&format!("<section id=\"{panel}\"")));
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
fn explicit_windows_write_index_and_only_link_to_their_five_files() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    Store::open(&db).unwrap();
    for window in ["4w", "12w", "all"] {
        let out = tmp.path().join(window);
        export(&db, &out, Some(window));
        assert_eq!(std::fs::read_dir(&out).unwrap().count(), 5);
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
    let tmp = tempfile::tempdir_in("target").unwrap();
    let config = std::env::var_os("USAGE_HABIT_REPORT_CONFIG").map(std::path::PathBuf::from);
    let allowlist = config
        .as_ref()
        .map(|p| {
            usage_habit_report::config::parse(&std::fs::read_to_string(p).unwrap())
                .unwrap()
                .public_projects
        })
        .unwrap_or_default();
    let output = export_with_config(&db, tmp.path(), None, config.as_deref());
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
    let fixture = tempfile::tempdir_in("target").unwrap();
    let fixture_db = fixture.path().join("fixture.db");
    let _fixture_store = fixture_store(&fixture_db);
    let fixture_out = fixture.path().join("out");
    export(&fixture_db, &fixture_out, None);
    assert_redacted_with_allowlist(
        &conn,
        tmp.path(),
        &page_vocabulary(&fixture_out),
        &allowlist,
    );
    let all = std::fs::read_to_string(tmp.path().join("index-all.html")).unwrap();
    for url in allowlist.values() {
        assert!(
            all.contains(&format!(
                "href=\"{}\" rel=\"noopener\">{}",
                html::esc(url),
                html::esc(usage_habit_report::public_projects::name(url))
            )),
            "allowlisted link missing"
        );
    }
}

#[test]
fn public_projects_keep_links_and_separate_rows_without_paths() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    let store = fixture_store(&db);
    store.conn.execute_batch("UPDATE sessions SET parent_session_id = NULL, is_subagent = 0;
        INSERT INTO sessions (tool, session_id, project, project_name) VALUES ('codex', 'fixture-third-id', '/home/demo/private-third', 'demo');
        INSERT INTO requests (tool, request_key, session_id, ts_ms, input_tokens, output_tokens, cost_usd) VALUES ('codex', 'fixture-third-request', 'fixture-third-id', 1792380600000, 20, 10, 0.1);
        UPDATE sessions SET project_name = 'public-repo' WHERE project != '/home/demo/private-third';").unwrap();
    let config = tmp.path().join("config.toml");
    let text = r#"[public_projects]
"/home/demo/private-orchard" = "https://github.com/demo/public-repo"
"/home/demo/private-violet" = "https://github.com/demo/public-repo"
malformed setting
"#;
    std::fs::write(&config, text).unwrap();
    let out = tmp.path().join("out");
    let result = export_with_config(&db, &out, None, Some(&config));
    assert!(
        String::from_utf8_lossy(&result.stderr)
            .contains("warning: ignoring malformed public_projects setting")
    );
    let allowlist = usage_habit_report::config::parse(text)
        .unwrap()
        .public_projects;
    assert_redacted_with_allowlist(&store.conn, &out, &BTreeSet::new(), &allowlist);
    let body = std::fs::read_to_string(out.join("index-all.html")).unwrap();
    assert_eq!(
        body.matches(
            "<a href=\"https://github.com/demo/public-repo\" rel=\"noopener\">public-repo</a>"
        )
        .count(),
        2
    );
    assert!(body.contains("project C"));
    assert!(body.contains("Linked projects are public on GitHub. Other project names are pseudonyms shared across this export. Tool shares are percentages of requests."));
    assert!(!body.contains("title=\"/"));
    // Allowlisting a name must never exempt its private path.
    std::fs::write(
        out.join("index-all.html"),
        format!("{body}/home/demo/private-orchard"),
    )
    .unwrap();
    assert!(
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            assert_redacted_with_allowlist(&store.conn, &out, &BTreeSet::new(), &allowlist);
        }))
        .is_err()
    );
}

#[test]
fn template_words_pass_without_exempting_unusual_project_words() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    let store = fixture_store(&db);
    store.conn.execute_batch("UPDATE sessions SET project = '/home/demo/project', project_name = 'project' WHERE is_subagent = 0; UPDATE sessions SET project = '/home/demo/sessions', project_name = 'sessions' WHERE is_subagent = 1;").unwrap();
    let out = tmp.path().join("out");
    export(&db, &out, None);
    assert_redacted(&store.conn, &out, &BTreeSet::new());

    store.conn.execute_batch("UPDATE sessions SET project = '/home/demo/zyqfolder', project_name = 'zyqfolder' WHERE is_subagent = 0;").unwrap();
    export(&db, &out, None);
    assert_redacted(&store.conn, &out, &BTreeSet::new());
    let page = out.join("index.html");
    let body = std::fs::read_to_string(&page).unwrap();
    std::fs::write(&page, format!("{body}<p>zyqfolder</p>")).unwrap();
    assert!(
        usage_habit_report::privacy::check(
            &store.conn,
            &out,
            None,
            &Default::default(),
            &BTreeSet::new()
        )
        .is_err()
    );
    // Caller vocabulary is unioned with the template vocabulary.
    usage_habit_report::privacy::check(
        &store.conn,
        &out,
        None,
        &Default::default(),
        &BTreeSet::from(["zyqfolder".into()]),
    )
    .unwrap();
    // Even an exempt template word cannot exempt a full path in an asset.
    std::fs::write(&page, "<script>/home/demo/sessions</script>").unwrap();
    assert!(
        usage_habit_report::privacy::check(
            &store.conn,
            &out,
            None,
            &Default::default(),
            &BTreeSet::new()
        )
        .is_err()
    );
}

#[test]
fn template_vocabulary_uses_maximal_ascii_words_and_redaction_constants() {
    let words = usage_habit_report::privacy::template_vocabulary(1792384200000).unwrap();
    for word in ["project", "unknown", "sessions", "models", "section"] {
        assert!(words.contains(word));
    }
    assert!(words.iter().all(
        |word| word.len() >= 3 && word.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
    ));
    assert!(!words.contains("zyqfolder"));
}
