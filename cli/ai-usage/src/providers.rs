//! One fetcher per provider. Each mirrors the T3 Code reader named in its
//! section and returns a `Report`, or an error string shown as "unavailable".

use std::collections::BTreeMap;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;
use std::time::Duration;

use chrono::{DateTime, Local, Months, TimeZone, Utc};
use serde_json::{Value, json};

use crate::report::{Report, Window, ago, until};

type Res<T> = Result<T, String>;

const HOUR: i64 = 3600;
const WEEK: i64 = 7 * 24 * HOUR;

pub const PROVIDERS: [(&str, fn() -> Res<Report>); 5] = [
    ("Claude", claude),
    ("Codex", codex),
    ("Antigravity", antigravity),
    ("ChatLLM", abacus),
    ("Zed", zed),
];

// ---------- HTTP and parsing helpers ----------

fn agent() -> ureq::Agent {
    ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(15)))
        .max_redirects(0)
        .build()
        .into()
}

fn read_json(result: Result<ureq::http::Response<ureq::Body>, ureq::Error>) -> Res<Value> {
    match result {
        Ok(mut resp) => resp
            .body_mut()
            .read_json::<Value>()
            .map_err(|e| format!("bad JSON: {e}")),
        Err(ureq::Error::StatusCode(code)) => Err(format!("HTTP {code}")),
        Err(e) => Err(e.to_string()),
    }
}

fn get_json(url: &str, headers: &[(&str, &str)]) -> Res<Value> {
    let mut req = agent().get(url);
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    read_json(req.call())
}

/// Runs two requests at once.
fn both<A: Send, B: Send>(a: impl FnOnce() -> Res<A> + Send, b: impl FnOnce() -> Res<B> + Send) -> (Res<A>, Res<B>) {
    std::thread::scope(|s| {
        let second = s.spawn(b);
        let first = a();
        (first, second.join().unwrap_or_else(|_| Err("request panicked".into())))
    })
}

fn parse_time(v: &Value) -> Option<DateTime<Utc>> {
    match v {
        Value::String(s) => DateTime::parse_from_rfc3339(s)
            .ok()
            .map(|t| t.with_timezone(&Utc)),
        Value::Number(n) => n
            .as_i64()
            .filter(|&s| s > 0)
            .and_then(|s| Utc.timestamp_opt(s, 0).single()),
        _ => None,
    }
}

fn home() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default())
}

fn cache_dir() -> PathBuf {
    if cfg!(target_os = "macos") {
        home().join("Library/Caches/ai-usage")
    } else {
        std::env::var("XDG_CACHE_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|_| home().join(".cache"))
            .join("ai-usage")
    }
}

/// T3 Code's userdata directory: `$T3_USERDATA`, else the personal build, else the stock one.
fn t3_userdata() -> Option<PathBuf> {
    if let Ok(dir) = std::env::var("T3_USERDATA") {
        return Some(PathBuf::from(dir));
    }
    [".t3-personal/userdata", ".t3/userdata"]
        .iter()
        .map(|p| home().join(p))
        .find(|p| p.is_dir())
}

/// A secret T3 stores in plaintext under `userdata/secrets/`.
fn t3_secret(file: &str) -> Option<String> {
    let text = std::fs::read_to_string(t3_userdata()?.join("secrets").join(file)).ok()?;
    let text = text.trim().to_string();
    (!text.is_empty()).then_some(text)
}

fn env_or_t3_secret(var: &str, file: &str) -> Option<String> {
    std::env::var(var)
        .ok()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| t3_secret(file))
}

fn duration_label(secs: i64) -> &'static str {
    match secs {
        s if s >= 30 * 24 * HOUR => "Monthly",
        s if s >= WEEK => "Weekly",
        _ => "Session (5h)",
    }
}

/// `Limit reset credits: 2 (details) · next expires in 3h 2m (Oct 22 12:00)`;
/// says so when the provider does not report an expiry.
fn reset_credit_note(count: i64, details: &[String], expires_at: Option<DateTime<Utc>>) -> String {
    let mut note = format!("Limit reset credits: {count}");
    if !details.is_empty() {
        note.push_str(&format!(" ({})", details.join("; ")));
    }
    match expires_at {
        Some(t) => note.push_str(&format!(
            " · next expires in {} ({})",
            until(t),
            t.with_timezone(&Local).format("%b %-d %H:%M")
        )),
        None => note.push_str(" · expiry not reported"),
    }
    note
}

// ---------- Claude (claudeUsageLimits.ts, claudeResetCredits.ts) ----------

/// Gap in remaining weekly percentage points between the overall and a
/// model's own weekly limit before suggesting a change in that model's use.
const MODEL_ADVICE_GAP: f64 = 20.0;

fn claude() -> Res<Report> {
    let out = Command::new("security")
        .args(["find-generic-password", "-s", "Claude Code-credentials", "-w"])
        .output()
        .map_err(|e| format!("could not run `security`: {e}"))?;
    if !out.status.success() {
        return Err("no Claude Code login in the keychain".into());
    }
    let creds: Value = serde_json::from_slice(&out.stdout).map_err(|e| e.to_string())?;
    let oauth = &creds["claudeAiOauth"];
    let token = oauth["accessToken"].as_str().ok_or("keychain entry has no access token")?;
    if let Some(exp) = oauth["expiresAt"].as_i64() {
        if exp <= Utc::now().timestamp_millis() {
            return Err("access token expired; run `claude` once to refresh it".into());
        }
    }

    let (usage, stale_note) = claude_usage(token)?;

    let mut report = Report {
        provider: "Claude",
        plan: oauth["subscriptionType"].as_str().map(str::to_string),
        ..Default::default()
    };
    report.notes.extend(stale_note);

    // `limits` lists every active window, including model-scoped weekly ones.
    let mut weekly_all: Option<f64> = None;
    let mut scoped: Vec<(String, f64)> = Vec::new();
    if let Some(limits) = usage["limits"].as_array() {
        for l in limits {
            let Some(pct) = l["percent"].as_f64() else { continue };
            let resets = parse_time(&l["resets_at"]);
            let w = match l["kind"].as_str().unwrap_or("") {
                "session" => Window::new("Session (5h)", pct, resets, Some(5 * HOUR)),
                "weekly_all" => {
                    weekly_all = Some(pct);
                    Window::new("Weekly", pct, resets, Some(WEEK))
                }
                kind => match l["scope"]["model"]["display_name"].as_str() {
                    Some(model) => {
                        scoped.push((model.to_string(), pct));
                        Window::new(format!("Weekly · {model}"), pct, resets, Some(WEEK)).pool(model)
                    }
                    None => Window::new(kind, pct, resets, None),
                },
            };
            report.windows.push(w);
        }
    } else {
        for (key, label, secs) in [("five_hour", "Session (5h)", 5 * HOUR), ("seven_day", "Weekly", WEEK)] {
            if let Some(pct) = usage[key]["utilization"].as_f64() {
                report.windows.push(Window::new(label, pct, parse_time(&usage[key]["resets_at"]), Some(secs)));
            }
        }
    }

    let extra = &usage["extra_usage"];
    if extra["is_enabled"].as_bool() == Some(true) {
        if let (Some(used), Some(limit)) = (extra["used_credits"].as_f64(), extra["monthly_limit"].as_f64()) {
            report.notes.push(format!("Extra usage: {used:.2} of {limit:.2} this month"));
        }
    }
    // The overage-included bucket is the first model-scoped one (T3 relies on the same).
    let scoped_model = scoped.first().map(|(m, _)| m.as_str());
    if let Some((count, details, expires)) = claude_reset_credits(&usage["cedar_ember"], scoped_model) {
        report.notes.push(reset_credit_note(count, &details, expires));
    }

    if let Some(all_used) = weekly_all {
        report.advice = model_advice(all_used, &scoped);
    }
    Ok(report)
}

/// Model usage also counts toward the overall weekly limit, so whichever has
/// less left is the one that stops that model. Advise only on a clear gap.
fn model_advice(weekly_used: f64, scoped: &[(String, f64)]) -> Vec<String> {
    let all_left = 100.0 - weekly_used;
    scoped
        .iter()
        .filter_map(|(model, used)| {
            let model_left = 100.0 - used;
            if model_left - all_left >= MODEL_ADVICE_GAP {
                Some(format!(
                    "The weekly limit runs out before {model}'s ({all_left:.0}% vs {model_left:.0}% left): \
                     use {model} freely for the rest of the week."
                ))
            } else if all_left - model_left >= MODEL_ADVICE_GAP {
                Some(format!(
                    "{model} runs out before the weekly limit ({model_left:.0}% vs {all_left:.0}% left): \
                     save {model} for harder tasks and use other models."
                ))
            } else {
                None
            }
        })
        .collect()
}

/// Reuse a Claude usage reading this young instead of asking again.
const CLAUDE_FRESH_SECS: i64 = 120;

/// The usage response, through a small cache. Anthropic rate-limits this
/// endpoint tightly (Claude Code and T3 poll it too), so a reading under two
/// minutes old is reused, and while rate-limited the last reading is shown
/// with a note giving its age. Returns the response and that note, if any.
fn claude_usage(token: &str) -> Res<(Value, Option<String>)> {
    let cache_file = cache_dir().join("claude-usage.json");
    let mut hasher = DefaultHasher::new();
    token.hash(&mut hasher);
    let key = format!("{:016x}", hasher.finish());
    let now = Utc::now().timestamp();

    let cache = std::fs::read_to_string(&cache_file)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(|c| c["key"].as_str() == Some(&key));
    let fetched_at = cache.as_ref().and_then(|c| c["fetched_at"].as_i64());
    let retry_at = cache.as_ref().and_then(|c| c["retry_at"].as_i64()).unwrap_or(0);
    let cached_usage = cache.as_ref().map(|c| c["usage"].clone()).filter(|u| u.is_object());

    if let (Some(usage), Some(at)) = (&cached_usage, fetched_at) {
        if now - at < CLAUDE_FRESH_SECS {
            return Ok((usage.clone(), None));
        }
    }
    let rate_limited = |retry_at: i64| -> Res<(Value, Option<String>)> {
        let retry = Utc.timestamp_opt(retry_at, 0).single().map(until).unwrap_or_default();
        match (&cached_usage, fetched_at.and_then(|at| Utc.timestamp_opt(at, 0).single())) {
            (Some(usage), Some(at)) => Ok((
                usage.clone(),
                Some(format!(
                    "Anthropic is rate limiting usage checks: showing data from {} ago, retrying in {retry}",
                    ago(at)
                )),
            )),
            _ => Err(format!("Anthropic is rate limiting usage checks; retry in {retry}")),
        }
    };
    if retry_at > now {
        return rate_limited(retry_at);
    }

    // `cedar_ember=1` adds the limit-reset credits. The server reports them
    // only to a Claude Code user agent; other clients are told "ineligible".
    let mut req = ureq::Agent::config_builder()
        .timeout_global(Some(Duration::from_secs(15)))
        .http_status_as_error(false)
        .build()
        .new_agent()
        .get("https://api.anthropic.com/api/oauth/usage?cedar_ember=1")
        .header("Authorization", &format!("Bearer {token}"))
        .header("anthropic-beta", "oauth-2025-04-20");
    if let Some(v) = claude_cli_version() {
        req = req.header("User-Agent", &format!("claude-cli/{v} (external, cli)"));
    }
    let mut resp = req.call().map_err(|e| e.to_string())?;
    match resp.status().as_u16() {
        200..=299 => {
            let usage: Value = resp.body_mut().read_json().map_err(|e| format!("bad JSON: {e}"))?;
            write_private(&cache_file, &json!({ "key": key, "fetched_at": now, "usage": usage }).to_string());
            Ok((usage, None))
        }
        429 => {
            let wait = resp
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.trim().parse::<i64>().ok())
                .unwrap_or(300);
            let retry_at = now + wait;
            let mut updated = cache.unwrap_or_else(|| json!({ "key": key }));
            updated["retry_at"] = json!(retry_at);
            write_private(&cache_file, &updated.to_string());
            rate_limited(retry_at)
        }
        code => Err(format!("HTTP {code}")),
    }
}

fn claude_cli_version() -> Option<String> {
    static VERSION: OnceLock<Option<String>> = OnceLock::new();
    VERSION
        .get_or_init(|| {
            let out = Command::new("claude").arg("--version").output().ok()?;
            let text = String::from_utf8_lossy(&out.stdout);
            let version = text.split_whitespace().next()?;
            version.starts_with(|c: char| c.is_ascii_digit()).then(|| version.to_string())
        })
        .clone()
}

/// Live reset credits, using T3's rules: a grant counts when it is not
/// paused, is usable now and has not ended, and only once the server names a
/// next grant. Details describe that next grant: what it resets, when it expires.
fn claude_reset_credits(
    block: &Value,
    scoped_model: Option<&str>,
) -> Option<(i64, Vec<String>, Option<DateTime<Utc>>)> {
    if block["eligible"].as_bool() != Some(true) {
        return None;
    }
    let now = Utc::now();
    let live: Vec<&Value> = block["grants"]
        .as_array()?
        .iter()
        .filter(|g| {
            g["paused"].as_bool() != Some(true)
                && g["usable_now"].as_bool() == Some(true)
                && (g["ends_at"].is_null() || parse_time(&g["ends_at"]).is_some_and(|t| t > now))
        })
        .collect();
    let next_id = block["next_grant_id"].as_str()?;
    let next = live.iter().find(|g| g["id"].as_str() == Some(next_id))?;
    let count = live.iter().filter_map(|g| g["resets_left"].as_i64()).sum::<i64>();
    if count <= 0 {
        return None;
    }

    let clears: Vec<String> = next["clears"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(|c| match c {
            "five_hour" => "session".to_string(),
            "seven_day" => "weekly".to_string(),
            "seven_day_overage_included" => format!("{} weekly", scoped_model.unwrap_or("model")),
            other => other.replace('_', " "),
        })
        .collect();
    let kind = next["type"].as_str().unwrap_or("limit");
    let details = vec![if clears.is_empty() {
        format!("{kind} reset")
    } else {
        format!("{kind} reset: {}", clears.join(", "))
    }];
    Some((count, details, parse_time(&next["ends_at"])))
}

// ---------- Codex (codexUsageLimits.ts) ----------

fn codex() -> Res<Report> {
    let codex_home = std::env::var("CODEX_HOME").map(PathBuf::from).unwrap_or_else(|_| home().join(".codex"));
    let text = std::fs::read_to_string(codex_home.join("auth.json"))
        .map_err(|_| "no Codex login (~/.codex/auth.json)".to_string())?;
    let auth: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let tokens = &auth["tokens"];
    let token = tokens["access_token"].as_str().ok_or("Codex is not signed in with ChatGPT")?;

    let mut headers = vec![
        ("Authorization", format!("Bearer {token}")),
        ("User-Agent", "codex-cli".to_string()),
    ];
    if let Some(account) = tokens["account_id"].as_str() {
        headers.push(("ChatGPT-Account-Id", account.to_string()));
    }
    let headers: Vec<(&str, &str)> = headers.iter().map(|(k, v)| (*k, v.as_str())).collect();
    // The usage endpoint reports only credit counts; the expiries come from
    // the endpoint the Codex app-server reads (backend-client rate_limit_resets.rs).
    let (usage, reset_credits) = both(
        || get_json("https://chatgpt.com/backend-api/wham/usage", &headers),
        || get_json("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", &headers),
    );
    let usage = usage.map_err(|e| {
        if e == "HTTP 401" { "token expired; run `codex` once to refresh it".into() } else { e }
    })?;

    let plan = usage["plan_type"].as_str().map(str::to_string);
    let mut report = Report { provider: "Codex", plan: plan.clone(), ..Default::default() };

    // Primary with no duration is 5h on paid plans and 30 days on Free/Go.
    let primary_fallback = if matches!(plan.as_deref(), Some("free" | "go")) { 30 * 24 * HOUR } else { 5 * HOUR };
    for (key, fallback) in [("primary_window", primary_fallback), ("secondary_window", WEEK)] {
        let w = &usage["rate_limit"][key];
        let Some(pct) = w["used_percent"].as_f64() else { continue };
        let secs = w["limit_window_seconds"].as_i64().filter(|&s| s > 0).unwrap_or(fallback);
        report.windows.push(Window::new(duration_label(secs), pct, parse_time(&w["reset_at"]), Some(secs)));
    }

    let credits = &usage["credits"];
    if credits["unlimited"].as_bool() == Some(true) {
        report.notes.push("Credits: unlimited".into());
    } else if let Some(balance) = credits["balance"].as_str().and_then(|b| b.parse::<f64>().ok()) {
        report.notes.push(format!("Credits: {balance:.2}"));
    }
    let resets = &usage["rate_limit_reset_credits"];
    if let Some(n) = resets["available_count"].as_i64().filter(|&n| n > 0) {
        let usable = resets["applicable_available_count"].as_i64().filter(|&a| a < n);
        let mut details: Vec<String> = usable.map(|a| format!("{a} usable now")).into_iter().collect();
        // As T3 does: the next to expire among the credits still available.
        let available: Vec<&Value> = reset_credits
            .as_ref()
            .ok()
            .and_then(|v| v["credits"].as_array())
            .into_iter()
            .flatten()
            .filter(|c| c["status"].as_str() == Some("available"))
            .collect();
        let expires = available.iter().filter_map(|c| parse_time(&c["expires_at"])).min();
        if let Some(title) = available.iter().find_map(|c| c["title"].as_str()) {
            details.insert(0, title.to_string());
        }
        report.notes.push(reset_credit_note(n, &details, expires));
    }
    Ok(report)
}

// ---------- Antigravity (antigravityUsageLimits.ts) ----------

const ANTIGRAVITY_ENDPOINTS: [&str; 2] = [
    "https://daily-cloudcode-pa.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
];

fn find_antigravity_token() -> Option<PathBuf> {
    let root = t3_userdata()?.join("providers/antigravity");
    std::fs::read_dir(root)
        .ok()?
        .flatten()
        .map(|e| e.path().join("antigravity-acp/acp_token.json"))
        .find(|p| p.is_file())
}

fn antigravity() -> Res<Report> {
    let path = find_antigravity_token().ok_or("no Antigravity sign-in found in T3 Code")?;
    let token: Value = serde_json::from_str(&std::fs::read_to_string(&path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let body = match token["project_id"].as_str() {
        Some(p) if !p.is_empty() => json!({ "project": p }),
        _ => json!({}),
    };

    let (access, cached) = antigravity_access_token(&token, &path, false)?;
    let mut windows = antigravity_windows(&access, &body);
    // A cached token Google has revoked early: get a fresh one and retry once.
    if windows.is_empty() && cached {
        let (access, _) = antigravity_access_token(&token, &path, true)?;
        windows = antigravity_windows(&access, &body);
    }
    if windows.is_empty() {
        return Err("Antigravity returned no quota information".into());
    }
    windows.sort_by(|a, b| a.label.cmp(&b.label));
    Ok(Report { provider: "Antigravity", windows, ..Default::default() })
}

/// An access token, from the cache while it has over a minute left, else
/// from Google. Google refresh tokens do not rotate, so refreshing is safe.
/// Returns whether the token came from the cache.
fn antigravity_access_token(token: &Value, path: &Path, force_refresh: bool) -> Res<(String, bool)> {
    let field = |k: &str| token[k].as_str().ok_or(format!("{} is missing {k}", path.display()));
    let (client_id, client_secret, refresh_token) =
        (field("client_id")?, field("client_secret")?, field("refresh_token")?);

    let mut hasher = DefaultHasher::new();
    (client_id, refresh_token).hash(&mut hasher);
    let key = format!("{:016x}", hasher.finish());
    let cache_file = cache_dir().join("antigravity-token.json");
    let now = Utc::now().timestamp();

    if !force_refresh {
        let cached = std::fs::read_to_string(&cache_file)
            .ok()
            .and_then(|t| serde_json::from_str::<Value>(&t).ok());
        if let Some(c) = cached {
            if c["key"].as_str() == Some(&key) && c["expires_at"].as_i64().is_some_and(|e| e > now + 60) {
                if let Some(access) = c["access_token"].as_str() {
                    return Ok((access.to_string(), true));
                }
            }
        }
    }

    let refreshed = read_json(agent().post("https://oauth2.googleapis.com/token").send_form([
        ("client_id", client_id),
        ("client_secret", client_secret),
        ("refresh_token", refresh_token),
        ("grant_type", "refresh_token"),
    ]))
    .map_err(|e| format!("token refresh failed: {e}"))?;
    let access = refreshed["access_token"].as_str().ok_or("token refresh returned no access token")?;
    let expires_at = now + refreshed["expires_in"].as_i64().unwrap_or(0);
    write_private(
        &cache_file,
        &json!({ "key": key, "access_token": access, "expires_at": expires_at }).to_string(),
    );
    Ok((access.to_string(), false))
}

/// Best-effort write readable only by the user; a failed cache write is not an error.
fn write_private(path: &Path, contents: &str) {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let Some(dir) = path.parent() else { return };
    if std::fs::create_dir_all(dir).is_err() {
        return;
    }
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path);
    if let Ok(mut f) = file {
        let _ = f.write_all(contents.as_bytes());
    }
}

fn antigravity_windows(access: &str, body: &Value) -> Vec<Window> {
    let windows = antigravity_fetch(access, body, "retrieveUserQuotaSummary", antigravity_summary_windows);
    if !windows.is_empty() {
        return windows;
    }
    antigravity_fetch(access, body, "fetchAvailableModels", antigravity_model_windows)
}

/// Queries both endpoints at once. They can report different quota groups,
/// so keep the most-used reading of each bucket, as T3 does.
fn antigravity_fetch(
    access: &str,
    body: &Value,
    op: &str,
    parse: fn(&Value) -> Vec<(String, Window)>,
) -> Vec<Window> {
    let arch = match std::env::consts::ARCH {
        "aarch64" => "arm64",
        "x86_64" => "x64",
        other => other,
    };
    let user_agent = format!(
        "antigravity/hub/2.24.0 (aidev_client; os_type={}; arch={arch}; cl=963137146)",
        std::env::consts::OS
    );
    let bearer = format!("Bearer {access}");

    let responses: Vec<Vec<(String, Window)>> = std::thread::scope(|s| {
        let handles: Vec<_> = ANTIGRAVITY_ENDPOINTS
            .iter()
            .map(|endpoint| {
                let (bearer, user_agent) = (&bearer, &user_agent);
                s.spawn(move || {
                    let resp = agent()
                        .post(&format!("{endpoint}/v1internal:{op}"))
                        .header("Authorization", bearer)
                        .header("User-Agent", user_agent)
                        .send_json(body);
                    read_json(resp).map(|v| parse(&v)).unwrap_or_default()
                })
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap_or_default()).collect()
    });

    let mut merged: BTreeMap<String, Window> = BTreeMap::new();
    for (id, w) in responses.into_iter().flatten() {
        if merged.get(&id).is_none_or(|prev| w.used_percent > prev.used_percent) {
            merged.insert(id, w);
        }
    }
    merged.into_values().collect()
}

fn quota_window(family: &str, span: &str, quota: &Value, secs: Option<i64>) -> Option<Window> {
    let remaining = quota["remainingFraction"].as_f64()?;
    let used = (1.0 - remaining.clamp(0.0, 1.0)) * 100.0;
    // An unspent window reports a synthetic reset of now + window length.
    let resets_at = if used > 0.0 { parse_time(&quota["resetTime"]) } else { None };
    Some(Window::new(format!("{family} · {span}"), used, resets_at, secs).pool(family))
}

fn antigravity_summary_windows(summary: &Value) -> Vec<(String, Window)> {
    let groups: Vec<Value> = match summary["groups"].as_array() {
        Some(g) if g.iter().any(|g| g["buckets"].as_array().is_some_and(|b| !b.is_empty())) => g.clone(),
        _ => vec![json!({ "buckets": summary["buckets"] })],
    };
    let mut out = Vec::new();
    for group in &groups {
        for bucket in group["buckets"].as_array().into_iter().flatten() {
            let Some(id) = bucket["bucketId"].as_str() else { continue };
            if bucket["disabled"].as_bool() == Some(true) {
                continue;
            }
            let family = if id.starts_with("gemini-argon-") {
                "Argon"
            } else if id.starts_with("gemini-") {
                "Gemini"
            } else if id.starts_with("3p-") {
                "Claude & GPT"
            } else {
                group["displayName"].as_str().unwrap_or(id).trim()
            };
            let (span, secs) = match bucket["window"].as_str() {
                Some("weekly") => ("Weekly", Some(WEEK)),
                Some("5h") => ("Session (5h)", Some(5 * HOUR)),
                _ => (bucket["displayName"].as_str().unwrap_or("Quota").trim(), None),
            };
            if let Some(w) = quota_window(family, span, bucket, secs) {
                out.push((id.to_string(), w));
            }
        }
    }
    out
}

/// Older accounts report per-model quotas without a window length.
fn antigravity_model_windows(models: &Value) -> Vec<(String, Window)> {
    let mut out: BTreeMap<String, Window> = BTreeMap::new();
    for (id, model) in models["models"].as_object().into_iter().flatten() {
        let provider = model["modelProvider"].as_str().unwrap_or("");
        let family = if provider == "MODEL_PROVIDER_GOOGLE" || id.starts_with("gemini-") {
            "Gemini"
        } else if provider == "MODEL_PROVIDER_ANTHROPIC" || id.starts_with("claude-") {
            "Claude"
        } else if provider == "MODEL_PROVIDER_OPENAI" || id.starts_with("gpt-") {
            "GPT"
        } else {
            continue;
        };
        for (suffix, key, span, secs) in [
            ("quota", "quotaInfo", "Quota", None),
            ("weekly", "weeklyQuotaInfo", "Weekly", Some(WEEK)),
        ] {
            let Some(w) = quota_window(family, span, &model[key], secs) else { continue };
            let wid = format!("{family}-{suffix}");
            if out.get(&wid).is_none_or(|prev| w.used_percent > prev.used_percent) {
                out.insert(wid, w);
            }
        }
    }
    out.into_iter().collect()
}

// ---------- ChatLLM / Abacus (abacusUsageLimits.ts) ----------

fn abacus() -> Res<Report> {
    let key = env_or_t3_secret("ABACUS_API_KEY", "provider-env-YWJhY3Vz-QUJBQ1VTX0FQSV9LRVk.bin")
        .ok_or("no ABACUS_API_KEY (env or T3 Code secret)")?;
    let bearer = format!("Bearer {key}");
    let headers = [("apiKey", key.as_str()), ("Authorization", bearer.as_str()), ("Accept", "application/json")];

    let (points, billing) = both(
        || get_json("https://apps.abacus.ai/api/_getOrganizationComputePoints", &headers),
        || {
            let mut req = agent().post("https://apps.abacus.ai/api/_getBillingInfo");
            for (k, v) in headers {
                req = req.header(k, v);
            }
            read_json(req.send_empty())
        },
    );
    let points = points?;
    if let Some(err) = points["error"].as_str() {
        return Err(err.to_string());
    }
    let billing = billing.unwrap_or(Value::Null);

    let r = &points["result"];
    let left = r["computePointsLeft"]
        .as_f64()
        .or_else(|| r["curr_month_avail_points"].as_f64())
        .ok_or("response has no remaining credit count")?;
    let total = r["totalComputePoints"].as_f64().filter(|&t| t > 0.0);
    let used = total.map_or(0.0, |t| (t - left) / t * 100.0);

    let resets_at = [&billing["result"]["nextBillingDate"], &r["nextBillingDate"], &r["resetAt"]]
        .into_iter()
        .find_map(parse_time);
    // Credits renew monthly on the billing date.
    let period = resets_at
        .and_then(|end| end.checked_sub_months(Months::new(1)).map(|start| (end - start).num_seconds()));

    let detail = match total {
        Some(t) => format!("{} / {} left", thousands(left), thousands(t)),
        None => format!("{} left", thousands(left)),
    };
    Ok(Report {
        provider: "ChatLLM",
        plan: billing["result"]["currentTier"].as_str().map(str::to_string),
        windows: vec![Window::new("Monthly credits", used, resets_at, period).detail(detail)],
        ..Default::default()
    })
}

fn thousands(n: f64) -> String {
    let digits = format!("{}", n.round() as i64);
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i) % 3 == 0 && c != '-' {
            out.push(',');
        }
        out.push(c);
    }
    out
}

// ---------- Zed (zedAccountUsage.ts) ----------

fn zed() -> Res<Report> {
    let session = env_or_t3_secret("ZED_DASHBOARD_SESSION", "provider-config-emVk-emVk-ZGFzaGJvYXJkU2Vzc2lvbg.bin")
        .ok_or("no Zed dashboard session (env or T3 Code secret)")?;
    let headers = [
        ("Accept", "application/json"),
        ("Cookie", session.as_str()),
        ("Origin", "https://dashboard.zed.dev"),
    ];
    let signed_out = |e: String| {
        if matches!(e.as_str(), "HTTP 401" | "HTTP 403" | "HTTP 302") {
            "dashboard session expired; replace it in T3's Zed settings".to_string()
        } else {
            e
        }
    };
    let (usage, sub) = both(
        || get_json("https://cloud.zed.dev/frontend/billing/usage", &headers),
        || get_json("https://cloud.zed.dev/frontend/billing/subscriptions/current", &headers),
    );
    let (usage, sub) = (usage.map_err(signed_out)?, sub.map_err(signed_out)?);

    let spend = &usage["current_usage"]["token_spend"];
    let spent = spend["spend_in_cents"].as_f64().ok_or("Zed reported no token spend")?;
    let limit = spend["limit_in_cents"].as_f64().filter(|&l| l > 0.0);

    // Zed keeps reporting the ended period for hours after the monthly rollover.
    let mut end = parse_time(&sub["subscription"]["period"]["end_at"]);
    while let Some(e) = end.filter(|e| *e <= Utc::now()) {
        end = e.checked_add_months(Months::new(1));
    }
    let period = end.and_then(|e| e.checked_sub_months(Months::new(1)).map(|s| (e - s).num_seconds()));

    let detail = match limit {
        Some(l) => format!("${:.2} of ${:.2}", spent / 100.0, l / 100.0),
        None => format!("${:.2}, no limit", spent / 100.0),
    };
    let used = limit.map_or(0.0, |l| spent / l * 100.0);
    Ok(Report {
        provider: "Zed",
        plan: sub["subscription"]["name"].as_str().map(str::to_string),
        windows: vec![Window::new("Monthly token spend", used, end, period).detail(detail)],
        ..Default::default()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scoped(pct: f64) -> Vec<(String, f64)> {
        vec![("Fable".to_string(), pct)]
    }

    #[test]
    fn advises_using_model_when_weekly_runs_out_first() {
        let advice = model_advice(90.0, &scoped(60.0));
        assert_eq!(advice.len(), 1);
        assert!(advice[0].contains("use Fable freely"), "{advice:?}");
        assert!(advice[0].contains("10% vs 40% left"), "{advice:?}");
    }

    #[test]
    fn advises_saving_model_when_it_runs_out_first() {
        let advice = model_advice(30.0, &scoped(95.0));
        assert!(advice[0].contains("save Fable"), "{advice:?}");
    }

    #[test]
    fn no_advice_for_small_gaps() {
        assert!(model_advice(7.0, &scoped(3.0)).is_empty());
        assert!(model_advice(50.0, &scoped(65.0)).is_empty());
    }
}
