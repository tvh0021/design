//! Live subscription readings sampled from `ai-usage --local --json`.
//!
//! Codex writes its limit readings into every rollout, but Claude Code only
//! records the moment a window is exhausted and Antigravity records nothing.
//! `ai-usage` asks each provider for the current used share of every window,
//! so sampling it on a schedule builds the same kind of series for all three
//! plans, including the per-model quota pools (Claude's Fable weekly pool,
//! Antigravity's Gemini, Argon, and Claude & GPT pools).
//!
//! Readings land in `live_limits`, which no log can rebuild, so `rebuild`
//! leaves the table alone. `--local` keeps them to this machine's accounts:
//! with a hosts config, plain `ai-usage` also lists other machines, whose
//! accounts (a second Antigravity sign-in, say) would mix into one series.

use crate::store::{self, LiveLimit, Store};
use anyhow::{Context, Result, bail};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::PathBuf;

/// Providers worth sampling; ChatLLM and Zed have no plan in the value report.
pub const PROVIDERS: [&str; 3] = ["claude", "codex", "antigravity"];

#[derive(Deserialize)]
struct Provider {
    provider: String,
    #[serde(default)]
    windows: Vec<Window>,
    /// Set when the provider could not be read, e.g. an expired login.
    error: Option<String>,
    #[serde(default)]
    notes: Vec<String>,
}

/// The note `ai-usage` adds when a rate limit makes it show an earlier
/// reading. Storing that reading under the current time would fake a flat
/// stretch, so the provider is skipped instead.
const STALE_NOTE: &str = "showing data from";

/// One sample's readings, plus the providers left out and why.
pub struct Parsed {
    pub readings: Vec<LiveLimit>,
    pub skipped: Vec<(&'static str, String)>,
}

#[derive(Deserialize)]
struct Window {
    used_percent: Option<f64>,
    /// RFC 3339, sometimes with microseconds.
    resets_at: Option<String>,
    window_seconds: Option<i64>,
    pool: Option<String>,
}

/// The store's tool name for an `ai-usage` provider.
fn tool_of(provider: &str) -> Option<&'static str> {
    match provider.to_ascii_lowercase().as_str() {
        "claude" => Some("claude-code"),
        "codex" => Some("codex"),
        "antigravity" => Some("antigravity"),
        _ => None,
    }
}

/// Every session or weekly window in `ai-usage --local --json` output, stamped `ts_ms`.
/// Monthly windows and unknown providers are skipped. A provider that reports
/// an error or an earlier reading is skipped and listed with the reason. Reset
/// times are rounded to the second: providers report them with jitter between
/// calls.
pub fn parse(json: &str, ts_ms: i64) -> Result<Parsed> {
    let providers: Vec<Provider> =
        serde_json::from_str(json).context("ai-usage output is not a JSON array of providers")?;
    let mut out = Vec::new();
    let mut skipped = Vec::new();
    for p in providers {
        let Some(tool) = tool_of(&p.provider) else {
            continue;
        };
        if let Some(err) = p.error.filter(|e| !e.trim().is_empty()) {
            skipped.push((tool, err.trim().to_owned()));
            continue;
        }
        if let Some(note) = p.notes.iter().find(|n| n.contains(STALE_NOTE)) {
            skipped.push((tool, note.trim().to_owned()));
            continue;
        }
        for w in p.windows {
            let Some(pct) = w.used_percent.filter(|p| p.is_finite()) else {
                continue;
            };
            let window = match w.window_seconds {
                Some(s) if s <= 24 * 3600 => "session",
                Some(s) if s <= 8 * 24 * 3600 => "weekly",
                _ => continue,
            };
            let resets_at_ms = w
                .resets_at
                .as_deref()
                .and_then(|t| chrono::DateTime::parse_from_rfc3339(t).ok())
                .map(|t| (t.timestamp_millis() + 500).div_euclid(1000) * 1000);
            out.push(LiveLimit {
                tool: tool.to_owned(),
                pool: w.pool.unwrap_or_default().trim().to_owned(),
                window,
                ts_ms,
                used_percent: pct.clamp(0.0, 100.0),
                resets_at_ms,
            });
        }
    }
    Ok(Parsed {
        readings: out,
        skipped,
    })
}

/// The `ai-usage` binary: `$USAGE_HABIT_REPORT_AI_USAGE`, else `ai-usage` on PATH.
pub fn ai_usage_bin() -> PathBuf {
    std::env::var_os("USAGE_HABIT_REPORT_AI_USAGE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("ai-usage"))
}

/// Run `ai-usage --local --json claude codex antigravity` and return its stdout.
pub fn fetch() -> Result<String> {
    let bin = ai_usage_bin();
    let out = std::process::Command::new(&bin)
        .args(["--local", "--json"])
        .args(PROVIDERS)
        .output()
        .with_context(|| {
            format!(
                "running {} (set USAGE_HABIT_REPORT_AI_USAGE to its path)",
                bin.display()
            )
        })?;
    if !out.status.success() && out.stdout.is_empty() {
        bail!(
            "{} exited with {}: {}",
            bin.display(),
            out.status,
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }
    String::from_utf8(out.stdout).context("ai-usage printed non-UTF-8 output")
}

/// Store one sample; returns a one-line summary such as
/// `limits: 10 readings (antigravity 5, claude-code 3, codex 2)`, followed by
/// `; skipped claude-code: <reason>` for each provider left out.
pub fn record(store: &mut Store, json: &str, ts_ms: i64) -> Result<String> {
    let Parsed { readings, skipped } = parse(json, ts_ms)?;
    let tx = store.conn.transaction()?;
    let n = store::record_live_limits(&tx, &readings)?;
    tx.commit()?;
    let mut by_tool: BTreeMap<&str, usize> = BTreeMap::new();
    for r in &readings {
        *by_tool.entry(r.tool.as_str()).or_default() += 1;
    }
    let parts: Vec<String> = by_tool.iter().map(|(t, n)| format!("{t} {n}")).collect();
    let mut line = format!(
        "limits: {n} readings at {} ({})",
        crate::query::rfc3339(ts_ms),
        if parts.is_empty() {
            "none".into()
        } else {
            parts.join(", ")
        }
    );
    for (tool, why) in &skipped {
        line.push_str(&format!("; skipped {tool}: {why}"));
    }
    Ok(line)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"[
      {"provider":"Claude","plan":"team","windows":[
        {"label":"Session (5h)","used_percent":70.0,"resets_at":"2026-10-23T04:10:00.095964Z","window_seconds":18000,"blocked":false},
        {"label":"Weekly · Fable","used_percent":38.0,"resets_at":"2026-10-27T00:00:00.096117Z","window_seconds":604800,"pool":"Fable","blocked":false}]},
      {"provider":"Antigravity","windows":[
        {"label":"Gemini · Session (5h)","used_percent":0.0,"window_seconds":18000,"pool":"Gemini","blocked":false},
        {"label":"Argon · Weekly","used_percent":100.7,"resets_at":"2026-10-24T16:26:40Z","window_seconds":604800,"pool":"Argon","blocked":true}]},
      {"provider":"ChatLLM","windows":[{"label":"Monthly credits","used_percent":82.0,"window_seconds":2678400}]},
      {"provider":"Codex","windows":[{"label":"Monthly","used_percent":5.0,"window_seconds":2678400}]}
    ]"#;

    #[test]
    fn parses_session_and_weekly_windows_with_pools() {
        let r = parse(SAMPLE, 1_000).unwrap();
        assert!(r.skipped.is_empty());
        let r = r.readings;
        let got: Vec<(&str, &str, &str, f64, Option<i64>)> = r
            .iter()
            .map(|l| {
                (
                    l.tool.as_str(),
                    l.pool.as_str(),
                    l.window,
                    l.used_percent,
                    l.resets_at_ms,
                )
            })
            .collect();
        assert_eq!(
            got,
            vec![
                ("claude-code", "", "session", 70.0, Some(1_792_728_600_000)),
                (
                    "claude-code",
                    "Fable",
                    "weekly",
                    38.0,
                    Some(1_793_059_200_000)
                ),
                ("antigravity", "Gemini", "session", 0.0, None),
                (
                    "antigravity",
                    "Argon",
                    "weekly",
                    100.0,
                    Some(1_792_859_200_000)
                ),
            ]
        );
        assert!(r.iter().all(|l| l.ts_ms == 1_000));
        assert!(parse("{}", 0).is_err());
    }

    #[test]
    fn tolerates_account_and_host_fields() {
        let json = r#"[
          {"provider":"Codex","account":"0123456789ab","signed_in":true,
           "hosts":[{"name":"mac","t3_environment":"MacBook Pro"}],
           "windows":[{"label":"Weekly","used_percent":5.0,"window_seconds":604800,"blocked":false}]},
          {"provider":"Host","hosts":[{"name":"rpi5","t3_environment":null}],"windows":[],
           "error":"unreachable","account":null,"signed_in":true}
        ]"#;
        let r = parse(json, 0).unwrap();
        assert!(r.skipped.is_empty(), "{:?}", r.skipped);
        let r = r.readings;
        assert_eq!(r.len(), 1);
        assert_eq!((r[0].tool.as_str(), r[0].window), ("codex", "weekly"));
    }

    #[test]
    fn record_stores_readings_and_merges_a_repeat() {
        let tmp = tempfile::tempdir().unwrap();
        let mut store = Store::open(&tmp.path().join("usage.db")).unwrap();
        let line = record(&mut store, SAMPLE, 5_000).unwrap();
        assert_eq!(
            line,
            "limits: 4 readings at 1970-01-01T00:00:05Z (antigravity 2, claude-code 2)"
        );
        record(&mut store, SAMPLE, 5_000).unwrap();
        let n: i64 = store
            .conn
            .query_row("SELECT COUNT(*) FROM live_limits", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 4);
    }

    #[test]
    fn skips_providers_with_errors_or_earlier_readings() {
        let json = r#"[
          {"provider":"Claude","windows":[],"error":"access token expired; run `claude` once to refresh it"},
          {"provider":"Codex","windows":[{"label":"Weekly","used_percent":40.0,"window_seconds":604800}],
           "notes":["Anthropic is rate limiting usage checks: showing data from 2h ago, retrying in 3m"]},
          {"provider":"Antigravity","windows":[{"label":"Gemini · Weekly","used_percent":5.0,"window_seconds":604800,"pool":"Gemini"}],
           "notes":["Extra usage: 1.00 of 5.00 this month"]},
          {"provider":"Zed","windows":[],"error":"not signed in"}
        ]"#;
        let tmp = tempfile::tempdir().unwrap();
        let mut store = Store::open(&tmp.path().join("usage.db")).unwrap();
        let line = record(&mut store, json, 5_000).unwrap();
        assert_eq!(
            line,
            "limits: 1 readings at 1970-01-01T00:00:05Z (antigravity 1); \
             skipped claude-code: access token expired; run `claude` once to refresh it; \
             skipped codex: Anthropic is rate limiting usage checks: showing data from 2h ago, retrying in 3m"
        );
    }
}
