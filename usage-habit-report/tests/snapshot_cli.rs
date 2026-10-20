//! Public export privacy checks never print private values, even on failure.
use rusqlite::{Connection, OpenFlags};
use std::{collections::BTreeSet, path::Path, process::Command};
use usage_habit_report::{serve::html, store::Store};

fn export(db: &Path, out: &Path, window: Option<&str>) -> std::process::Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"));
    cmd.arg("--db")
        .arg(db)
        .arg("snapshot")
        .arg("--out")
        .arg(out);
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

fn assert_redacted(conn: &Connection, out: &Path) {
    // Include raw and HTML-escaped forms, folder names derived from the path,
    // stored project names, cwd, and T3 titles as well as the required fields.
    let columns = [
        "session_id",
        "t3_thread_id",
        "project",
        "project_name",
        "title",
        "cwd",
        "t3_title",
    ];
    let mut categories = Vec::new();
    for column in columns {
        let mut stmt = conn.prepare(&format!("SELECT DISTINCT {column} FROM sessions WHERE {column} IS NOT NULL AND {column} != ''")).unwrap();
        let values: Vec<String> = stmt
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let mut needles = BTreeSet::new();
        for value in values {
            let value = if column == "title" || column == "t3_title" {
                value.chars().take(20).collect::<String>()
            } else {
                value
            };
            needles.insert(html::esc(&value));
            needles.insert(value.clone());
            if column == "project"
                && let Some(name) = Path::new(&value).file_name().and_then(|s| s.to_str())
            {
                needles.insert(html::esc(name));
                needles.insert(name.to_string());
            }
        }
        categories.push((column, needles));
    }
    let files: Vec<_> = std::fs::read_dir(out)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(files.len(), 12);
    let mut matches = vec![0usize; categories.len()];
    for file in &files {
        let body = std::fs::read_to_string(file).unwrap();
        for (i, (_, needles)) in categories.iter().enumerate() {
            matches[i] += needles.iter().filter(|s| body.contains(s.as_str())).count();
        }
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
    for ((category, needles), count) in categories.iter().zip(&matches) {
        eprintln!(
            "redaction {category}: {} candidates, {count} matches; {} candidates also occur in shared CSS, {} in shared JS",
            needles.len(),
            needles
                .iter()
                .filter(|s| html::CSS.contains(s.as_str()))
                .count(),
            needles
                .iter()
                .filter(|s| html::JS.contains(s.as_str()))
                .count()
        );
    }
    assert_eq!(
        matches.iter().sum::<usize>(),
        0,
        "private values found; category counts above contain no private strings"
    );
}

#[test]
fn fixture_snapshot_is_redacted_offline_and_linked() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("usage.db");
    let store = Store::open(&db).unwrap();
    store.conn.execute_batch(
        "INSERT INTO sessions (tool, session_id, parent_session_id, is_subagent, title, cwd, project, project_name, t3_thread_id, t3_title, started_at_ms, ended_at_ms, label) VALUES
        ('codex', 'fixture-session-private-id', NULL, 0, 'Fixture private <title> & opening prompt', '/synthetic/private-orchard/work', '/synthetic/private-orchard', 'private-orchard', 'fixture-t3-private-id', 'Fixture private T3 title', 1792380600000, 1792384200000, 'coding'),
        ('codex', 'fixture-child-private-id', 'fixture-session-private-id', 1, 'Fixture child private title', '/synthetic/private-violet', '/synthetic/private-violet', 'private-violet', 'fixture-child-t3-private-id', 'Fixture child T3 private title', 1792380601000, 1792384200000, NULL);
        INSERT INTO requests (tool, request_key, session_id, is_subagent, ts_ms, canonical_model, input_tokens, output_tokens, cost_usd) VALUES
        ('codex', 'fixture-private-request-key', 'fixture-session-private-id', 0, 1792380600000, 'synthetic-model', 100, 50, 1.49),
        ('codex', 'fixture-child-request-key', 'fixture-child-private-id', 1, 1792380601000, 'synthetic-model', 50, 20, 0.49);"
    ).unwrap();
    let out = tmp.path().join("out");
    export(&db, &out, None);
    assert_redacted(&store.conn, &out);
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
    let db = Path::new(&home).join(".local/share/usage-habit-report/usage.db");
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
    assert_redacted(&conn, tmp.path());
}
