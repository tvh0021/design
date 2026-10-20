//! CLI behaviour of pricing: offline snapshot use, cost in `query summary`,
//! and unknown names surfacing in `query models`. No test touches the network.

use rusqlite::params;
use usage_habit_report::store::Store;

struct Out {
    stdout: String,
    stderr: String,
}

fn run(tmp: &std::path::Path, args: &[&str]) -> Out {
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
        .arg("--db")
        .arg(tmp.join("usage.db"))
        .args(args)
        .env("XDG_CONFIG_HOME", tmp.join("config"))
        .env("HOME", tmp) // the catch-up scan must not read the real harness folders
        .env_remove("CODEX_HOME")
        .env_remove("USAGE_HABIT_REPORT_OFFLINE")
        .output()
        .unwrap();
    let o = Out {
        stdout: String::from_utf8(out.stdout).unwrap(),
        stderr: String::from_utf8(out.stderr).unwrap(),
    };
    assert!(out.status.success(), "{}", o.stderr);
    o
}

fn seed(tmp: &std::path::Path) {
    let store = Store::open(&tmp.join("usage.db")).unwrap();
    let rows: &[(&str, Option<&str>, [i64; 4])] = &[
        ("a", Some("claude-opus-5-5"), [1_000_000, 0, 0, 0]),
        ("b", Some("claude-opus-5-5-high"), [0, 0, 0, 1_000_000]),
        ("c", Some("MODEL_PLACEHOLDER_M26"), [5, 5, 5, 5]),
        ("d", None, [1, 1, 1, 1]),
    ];
    for (key, model, t) in rows {
        store
            .conn
            .execute(
                "INSERT INTO requests (tool, request_key, session_id, ts_ms, model, input_tokens,
                     cache_read_input_tokens, cache_creation_input_tokens, output_tokens)
                 VALUES ('antigravity', ?1, 's', 0, ?2, ?3, ?4, ?5, ?6)",
                params![key, model, t[0], t[1], t[2], t[3]],
            )
            .unwrap();
    }
}

#[test]
fn offline_summary_uses_snapshot_and_reports_estimated_cost() {
    let tmp = tempfile::tempdir().unwrap();
    seed(tmp.path());
    let o = run(tmp.path(), &["--offline", "query", "summary", "--json"]);
    assert!(!o.stderr.contains("refreshing"), "{}", o.stderr);
    let v: serde_json::Value = serde_json::from_str(&o.stdout).unwrap();
    assert!(v["rates_fetched_at"].is_string());
    let groups = v["groups"].as_array().unwrap();
    let group = |m: &str| groups.iter().find(|g| g["model"] == m).unwrap();
    // The `-high` variant folds into its canonical name: one group, two requests,
    // priced as 1M input plus 1M output tokens.
    assert!(groups.iter().all(|g| g["model"] != "claude-opus-5-5-high"));
    let opus = group("claude-opus-5-5");
    assert_eq!(opus["requests"], 2);
    let a = opus["estimated_cost_usd"].as_f64().unwrap();
    assert!(a > 0.0, "{a}");
    assert!(group("MODEL_PLACEHOLDER_M26")["estimated_cost_usd"].is_null());
    assert_eq!(group("MODEL_PLACEHOLDER_M26")["unpriced_requests"], 1);
    let total = v["totals"]["estimated_cost_usd"].as_f64().unwrap();
    assert!((total - a).abs() < 1e-9);
    assert_eq!(v["totals"]["unpriced_requests"], 2);

    let o = run(tmp.path(), &["--offline", "query", "summary"]);
    assert!(o.stdout.contains("est. cost"), "{}", o.stdout);
    assert!(o.stdout.contains("excludes 2 requests"), "{}", o.stdout);
}

#[test]
fn query_models_surfaces_unpriced_names() {
    let tmp = tempfile::tempdir().unwrap();
    seed(tmp.path());
    let o = run(tmp.path(), &["--offline", "query", "models", "--json"]);
    let rows: Vec<serde_json::Value> = serde_json::from_str(&o.stdout).unwrap();
    let row = |m: serde_json::Value| rows.iter().find(|r| r["model"] == m).unwrap();
    let high = row("claude-opus-5-5-high".into());
    assert_eq!(high["canonical_model"], "claude-opus-5-5");
    assert_eq!(high["priced"], true);
    let placeholder = row("MODEL_PLACEHOLDER_M26".into());
    assert!(placeholder["canonical_model"].is_null());
    assert_eq!(placeholder["priced"], false);
    assert_eq!(row(serde_json::Value::Null)["priced"], false);
    // Unpriced first so they are the first thing a reader sees.
    assert_eq!(rows[0]["priced"], false);
}

#[test]
fn fresh_rates_file_is_used_without_fetching() {
    let tmp = tempfile::tempdir().unwrap();
    seed(tmp.path());
    let dir = tmp.path().join("config/usage-habit-report");
    std::fs::create_dir_all(&dir).unwrap();
    let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
    let file = serde_json::json!({
        "fetched_at": now,
        "source": "test",
        "models": {"claude-opus-5-5": {
            "litellm_provider": "anthropic", "mode": "chat",
            "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6
        }}
    });
    std::fs::write(dir.join("rates.json"), file.to_string()).unwrap();
    // Not offline: a fresh file means no refresh line and no network.
    let o = run(tmp.path(), &["rates", "apply", "--all"]);
    assert_eq!(o.stderr, "");
    assert!(o.stdout.contains("(rates.json)"), "{}", o.stdout);
    let o = run(tmp.path(), &["query", "summary", "--json"]);
    let v: serde_json::Value = serde_json::from_str(&o.stdout).unwrap();
    // 1M input at $1/M + 1M output at $2/M.
    let total = v["totals"]["estimated_cost_usd"].as_f64().unwrap();
    assert!((total - 3.0).abs() < 1e-9, "{total}");
}
