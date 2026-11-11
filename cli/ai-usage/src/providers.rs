//! One fetcher per provider. Each mirrors the T3 Code reader named in its
//! section and returns a `Report`, or a `Failure` shown as "unavailable".
//! Every report carries a fingerprint of the signed-in account, so readings
//! from several machines can be matched up without revealing who it is.

use std::collections::BTreeMap;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::OnceLock;
use std::time::Duration;

use chrono::{DateTime, Local, Months, TimeZone, Utc};
use serde_json::{Value, json};

use crate::report::{Report, Window, ago, fingerprint, until};

type Res<T> = Result<T, String>;

/// Why a provider has no reading.
#[derive(Debug)]
pub struct Failure {
    pub message: String,
    /// False when this machine simply has no sign-in for the provider.
    pub signed_in: bool,
}

impl Failure {
    fn signed_out(message: impl Into<String>) -> Self {
        Failure { message: message.into(), signed_in: false }
    }
}

impl From<String> for Failure {
    fn from(message: String) -> Self {
        Failure { message, signed_in: true }
    }
}

impl From<&str> for Failure {
    fn from(message: &str) -> Self {
        message.to_string().into()
    }
}

type Probe = fn() -> Result<Report, Failure>;

const HOUR: i64 = 3600;
const WEEK: i64 = 7 * 24 * HOUR;

pub const PROVIDERS: [(&str, Probe); 5] = [
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

fn non_empty(val: Option<&str>) -> Option<&str> {
    val.map(str::trim).filter(|v| !v.is_empty())
}

fn cache_dir() -> PathBuf {
    cache_dir_for(cfg!(target_os = "macos"), std::env::var("XDG_CACHE_HOME").ok().as_deref(), &home())
}

fn cache_dir_for(is_macos: bool, xdg_cache_home: Option<&str>, home: &Path) -> PathBuf {
    if is_macos {
        home.join("Library/Caches/ai-usage")
    } else {
        non_empty(xdg_cache_home)
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".cache"))
            .join("ai-usage")
    }
}

/// T3 Code's userdata directory: `$T3_USERDATA`, `$T3CODE_HOME/userdata`, else the personal or stock build.
fn t3_userdata() -> Option<PathBuf> {
    t3_userdata_from(
        std::env::var("T3_USERDATA").ok().as_deref(),
        std::env::var("T3CODE_HOME").ok().as_deref(),
        &home(),
    )
}

fn t3_userdata_from(t3_userdata: Option<&str>, t3code_home: Option<&str>, home: &Path) -> Option<PathBuf> {
    if let Some(dir) = non_empty(t3_userdata) {
        return Some(PathBuf::from(dir));
    }
    if let Some(dir) = non_empty(t3code_home) {
        return Some(PathBuf::from(dir).join("userdata"));
    }
    [".t3-personal/userdata", ".t3/userdata"]
        .iter()
        .map(|p| home.join(p))
        .find(|p| p.is_dir())
}

/// A secret T3 stores in plaintext under `userdata/secrets/`.
fn t3_secret(file: &str) -> Res<Option<String>> {
    match t3_userdata() {
        Some(userdata) => t3_secret_in(&userdata, file),
        None => Ok(None),
    }
}

/// `None` when the secret file is missing or empty; any other read failure is an error.
fn t3_secret_in(userdata: &Path, file: &str) -> Res<Option<String>> {
    let path = userdata.join("secrets").join(file);
    let text = match std::fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("could not read {}: {e}", path.display())),
    };
    let text = text.trim().to_string();
    Ok((!text.is_empty()).then_some(text))
}

fn env_or_t3_secret(var: &str, file: &str) -> Res<Option<String>> {
    match std::env::var(var).ok().filter(|v| !v.trim().is_empty()) {
        Some(v) => Ok(Some(v)),
        None => t3_secret(file),
    }
}

/// A credential file's text. Only a missing file means "not signed in";
/// a file that exists but cannot be read is a real failure.
fn read_credential(path: &Path, missing: &str) -> Result<String, Failure> {
    std::fs::read_to_string(path).map_err(|e| match e.kind() {
        std::io::ErrorKind::NotFound => Failure::signed_out(missing),
        _ => format!("could not read {}: {e}", path.display()).into(),
    })
}

/// `security` exits 44 when the keychain has no such item; anything else
/// (a locked keychain, a denied prompt) is a real failure.
fn keychain_failure(code: Option<i32>) -> Failure {
    match code {
        Some(44) => Failure::signed_out("no Claude Code login in the keychain"),
        Some(c) => format!("could not read the keychain (`security` exit {c})").into(),
        None => "could not read the keychain (`security` was killed)".into(),
    }
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

fn claude_config_dir_from(claude_config_dir: Option<&str>, home: &Path) -> PathBuf {
    non_empty(claude_config_dir)
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".claude"))
}

fn claude_credentials_file(config_dir: &Path) -> Result<Value, Failure> {
    let text = read_credential(
        &config_dir.join(".credentials.json"),
        "no Claude Code login (~/.claude/.credentials.json)",
    )?;
    Ok(serde_json::from_str(&text).map_err(|e| e.to_string())?)
}

fn claude_keychain_credentials() -> Result<Value, Failure> {
    let out = Command::new("security")
        .args(["find-generic-password", "-s", "Claude Code-credentials", "-w"])
        .output()
        .map_err(|e| format!("could not run `security`: {e}"))?;
    if !out.status.success() {
        return Err(keychain_failure(out.status.code()));
    }
    Ok(serde_json::from_slice(&out.stdout).map_err(|e| e.to_string())?)
}

fn claude_credentials_with(
    is_macos: bool,
    claude_config_dir: Option<&str>,
    home: &Path,
    keychain: impl FnOnce() -> Result<Value, Failure>,
) -> Result<(Value, &'static str), Failure> {
    let dir = claude_config_dir_from(claude_config_dir, home);
    if !is_macos || (non_empty(claude_config_dir).is_some() && dir.join(".credentials.json").is_file()) {
        claude_credentials_file(&dir).map(|v| (v, "credentials file"))
    } else {
        keychain().map(|v| (v, "keychain entry"))
    }
}

fn claude_oauth_from_creds(
    creds: &Value,
    now_ms: i64,
    source: &str,
) -> Result<(String, Option<String>), Failure> {
    let oauth = &creds["claudeAiOauth"];
    let token = oauth["accessToken"]
        .as_str()
        .filter(|t| !t.trim().is_empty())
        .ok_or_else(|| Failure::signed_out(format!("{source} has no access token")))?;
    if let Some(exp) = oauth["expiresAt"].as_i64() {
        if exp <= now_ms {
            return Err("access token expired; run `claude` once to refresh it".into());
        }
    }
    let plan = oauth["subscriptionType"].as_str().map(str::to_string);
    Ok((token.to_string(), plan))
}

/// Claude Code's account record: `$CLAUDE_CONFIG_DIR/.claude.json`, else `~/.claude.json`.
fn claude_account_file(claude_config_dir: Option<&str>, home: &Path) -> PathBuf {
    match non_empty(claude_config_dir) {
        Some(dir) => PathBuf::from(dir).join(".claude.json"),
        None => home.join(".claude.json"),
    }
}

/// The account fingerprint from `.claude.json`: the account within its organization.
fn claude_account_from(config: &Value) -> Option<String> {
    let account = &config["oauthAccount"];
    let id = non_empty(account["accountUuid"].as_str())?;
    let org = account["organizationUuid"].as_str().unwrap_or("");
    Some(fingerprint("claude", &format!("{id}:{org}")))
}

fn claude_account(claude_config_dir: Option<&str>) -> Option<String> {
    let text = std::fs::read_to_string(claude_account_file(claude_config_dir, &home())).ok()?;
    claude_account_from(&serde_json::from_str(&text).ok()?)
}

fn claude() -> Result<Report, Failure> {
    let config_dir = std::env::var("CLAUDE_CONFIG_DIR").ok();
    let (creds, source) = claude_credentials_with(
        cfg!(target_os = "macos"),
        config_dir.as_deref(),
        &home(),
        claude_keychain_credentials,
    )?;
    let (token, plan) = claude_oauth_from_creds(&creds, Utc::now().timestamp_millis(), source)?;

    let (usage, stale_note) = claude_usage(&token)?;

    let mut report = Report {
        provider: "Claude".into(),
        plan,
        account: claude_account(config_dir.as_deref()),
        ..Default::default()
    };
    report.stale = stale_note.is_some();
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
            let out = Command::new("claude")
                .arg("--version")
                .output()
                .or_else(|_| Command::new(home().join(".local/bin/claude")).arg("--version").output())
                .ok()?;
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

fn codex_home_from(codex_home: Option<&str>, home: &Path) -> PathBuf {
    non_empty(codex_home)
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".codex"))
}

struct CodexAuth {
    token: String,
    account_id: Option<String>,
    /// Fingerprint of the ChatGPT user within the workspace.
    account: Option<String>,
}

fn codex_auth_from_dir(codex_home: &Path) -> Result<CodexAuth, Failure> {
    let text = read_credential(&codex_home.join("auth.json"), "no Codex login (~/.codex/auth.json)")?;
    let auth: Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
    let tokens = &auth["tokens"];
    let token = tokens["access_token"]
        .as_str()
        .filter(|t| !t.trim().is_empty())
        .ok_or_else(|| Failure::signed_out("Codex is not signed in with ChatGPT"))?;
    let account_id = tokens["account_id"].as_str().map(str::to_string);
    let account = tokens["id_token"]
        .as_str()
        .and_then(codex_user_from_id_token)
        .map(|user| fingerprint("codex", &format!("{user}:{}", account_id.as_deref().unwrap_or(""))));
    Ok(CodexAuth { token: token.to_string(), account_id, account })
}

/// The ChatGPT user in an ID token: the OpenAI auth claim's `chatgpt_user_id`,
/// else its `user_id`, else the token subject. The signature is not checked:
/// the token only names the account, it grants nothing here.
fn codex_user_from_id_token(jwt: &str) -> Option<String> {
    let payload = base64url_decode(jwt.split('.').nth(1)?)?;
    let claims: Value = serde_json::from_slice(&payload).ok()?;
    let auth = &claims["https://api.openai.com/auth"];
    [&auth["chatgpt_user_id"], &auth["user_id"], &claims["sub"]]
        .into_iter()
        .find_map(|v| non_empty(v.as_str()))
        .map(str::to_string)
}

/// Unpadded base64url, as used in JWTs.
fn base64url_decode(text: &str) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(text.len() * 3 / 4);
    let (mut acc, mut bits) = (0u32, 0u32);
    for c in text.trim_end_matches('=').bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'-' | b'+' => 62,
            b'_' | b'/' => 63,
            _ => return None,
        };
        acc = (acc << 6) | u32::from(v);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Some(out)
}

fn codex() -> Result<Report, Failure> {
    let codex_home = codex_home_from(std::env::var("CODEX_HOME").ok().as_deref(), &home());
    let CodexAuth { token, account_id, account } = codex_auth_from_dir(&codex_home)?;

    let mut headers = vec![
        ("Authorization", format!("Bearer {token}")),
        ("User-Agent", "codex-cli".to_string()),
    ];
    if let Some(account) = account_id {
        headers.push(("ChatGPT-Account-Id", account));
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
    let mut report = Report { provider: "Codex".into(), plan: plan.clone(), account, ..Default::default() };

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

fn find_antigravity_token() -> Res<Option<PathBuf>> {
    match t3_userdata() {
        Some(userdata) => find_antigravity_token_in(&userdata),
        None => Ok(None),
    }
}

/// `None` when T3 has no Antigravity provider directory or no token in it.
fn find_antigravity_token_in(userdata: &Path) -> Res<Option<PathBuf>> {
    let root = userdata.join("providers/antigravity");
    let entries = match std::fs::read_dir(&root) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("could not read {}: {e}", root.display())),
    };
    Ok(entries
        .flatten()
        .map(|e| e.path().join("antigravity-acp/acp_token.json"))
        .find(|p| p.is_file()))
}

fn antigravity() -> Result<Report, Failure> {
    let path = find_antigravity_token()?.ok_or_else(|| Failure::signed_out("no Antigravity sign-in found in T3 Code"))?;
    let token: Value = serde_json::from_str(&std::fs::read_to_string(&path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let body = match token["project_id"].as_str() {
        Some(p) if !p.is_empty() => json!({ "project": p }),
        _ => json!({}),
    };

    let (mut access, cached) = antigravity_access_token(&token, &path, false)?;
    let mut windows = antigravity_windows(&access, &body);
    // A cached token Google has revoked early: get a fresh one and retry once.
    if windows.is_empty() && cached {
        access = antigravity_access_token(&token, &path, true)?.0;
        windows = antigravity_windows(&access, &body);
    }
    if windows.is_empty() {
        return Err("Antigravity returned no quota information".into());
    }
    windows.sort_by(|a, b| a.label.cmp(&b.label));
    let account = antigravity_account(&access, &antigravity_key(&token, &path)?);
    Ok(Report { provider: "Antigravity".into(), windows, account, ..Default::default() })
}

/// Cache key for one sign-in: a hash of its client id and refresh token.
fn antigravity_key(token: &Value, path: &Path) -> Res<String> {
    let field = |k: &str| token[k].as_str().ok_or(format!("{} is missing {k}", path.display()));
    let mut hasher = DefaultHasher::new();
    (field("client_id")?, field("refresh_token")?).hash(&mut hasher);
    Ok(format!("{:016x}", hasher.finish()))
}

/// The Google account's fingerprint, from Google's user id. The project id in
/// the token file is shared between accounts, so it cannot tell them apart.
/// Asked once per sign-in and cached as the fingerprint only; `None` if Google
/// does not answer.
fn antigravity_account(access: &str, key: &str) -> Option<String> {
    let cache_file = cache_dir().join("antigravity-account.json");
    let cached = std::fs::read_to_string(&cache_file)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(|c| c["key"].as_str() == Some(key))
        .and_then(|c| c["account"].as_str().map(str::to_string));
    if cached.is_some() {
        return cached;
    }
    let info = get_json(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        &[("Authorization", &format!("Bearer {access}"))],
    )
    .ok()?;
    let account = antigravity_account_from(&info)?;
    write_private(&cache_file, &json!({ "key": key, "account": account }).to_string());
    Some(account)
}

fn antigravity_account_from(userinfo: &Value) -> Option<String> {
    non_empty(userinfo["id"].as_str()).map(|id| fingerprint("antigravity", id))
}

/// An access token, from the cache while it has over a minute left, else
/// from Google. Google refresh tokens do not rotate, so refreshing is safe.
/// Returns whether the token came from the cache.
fn antigravity_access_token(token: &Value, path: &Path, force_refresh: bool) -> Res<(String, bool)> {
    let field = |k: &str| token[k].as_str().ok_or(format!("{} is missing {k}", path.display()));
    let (client_id, client_secret, refresh_token) =
        (field("client_id")?, field("client_secret")?, field("refresh_token")?);

    let key = antigravity_key(token, path)?;
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

fn abacus() -> Result<Report, Failure> {
    let key = env_or_t3_secret("ABACUS_API_KEY", "provider-env-YWJhY3Vz-QUJBQ1VTX0FQSV9LRVk.bin")?
        .ok_or_else(|| Failure::signed_out("no ABACUS_API_KEY (env or T3 Code secret)"))?;
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
        return Err(err.into());
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
        provider: "ChatLLM".into(),
        plan: billing["result"]["currentTier"].as_str().map(str::to_string),
        account: Some(abacus_account_from(&billing, &key)),
        windows: vec![Window::new("Monthly credits", used, resets_at, period).detail(detail)],
        ..Default::default()
    })
}

/// Abacus shows an API key no account id, and each machine may hold its own
/// key, so the subscription's start time names the account; the key stands
/// in when billing could not be read.
fn abacus_account_from(billing: &Value, key: &str) -> String {
    match non_empty(billing["result"]["subscriptionStartTime"].as_str()) {
        Some(start) => fingerprint("chatllm", &format!("subscription:{start}")),
        None => fingerprint("chatllm", &format!("key:{key}")),
    }
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

fn zed() -> Result<Report, Failure> {
    let session = env_or_t3_secret("ZED_DASHBOARD_SESSION", "provider-config-emVk-emVk-ZGFzaGJvYXJkU2Vzc2lvbg.bin")?
        .ok_or_else(|| Failure::signed_out("no Zed dashboard session (env or T3 Code secret)"))?;
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
        provider: "Zed".into(),
        plan: sub["subscription"]["name"].as_str().map(str::to_string),
        account: Some(zed_account_from(&sub, &session)),
        windows: vec![Window::new("Monthly token spend", used, end, period).detail(detail)],
        ..Default::default()
    })
}

/// The subscription id names the account; the session cookie stands in when it is missing.
fn zed_account_from(sub: &Value, session: &str) -> String {
    match &sub["subscription"]["id"] {
        Value::String(id) if !id.trim().is_empty() => fingerprint("zed", id.trim()),
        Value::Number(id) => fingerprint("zed", &id.to_string()),
        _ => fingerprint("zed", &format!("session:{session}")),
    }
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

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!(
                "ai-usage-test-{name}-{}-{}",
                std::process::id(),
                Utc::now().timestamp_nanos_opt().unwrap_or_default()
            ));
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn claude_linux_reads_default_and_custom_config_dir() {
        let tmp = TempDir::new("claude-linux");
        let default_dir = tmp.path().join(".claude");
        std::fs::create_dir_all(&default_dir).unwrap();
        write_private(
            &default_dir.join(".credentials.json"),
            r#"{"claudeAiOauth":{"accessToken":"linux-default-tok","expiresAt":4102444800000,"subscriptionType":"max"}}"#,
        );

        let (creds, source) = claude_credentials_with(false, None, tmp.path(), || {
            Err("keychain should not run on Linux".into())
        })
        .unwrap();
        assert_eq!(source, "credentials file");
        let (tok, plan) = claude_oauth_from_creds(&creds, 1_700_000_000_000, source).unwrap();
        assert_eq!(tok, "linux-default-tok");
        assert_eq!(plan.as_deref(), Some("max"));

        let custom_dir = tmp.path().join("custom-claude");
        std::fs::create_dir_all(&custom_dir).unwrap();
        write_private(
            &custom_dir.join(".credentials.json"),
            r#"{"claudeAiOauth":{"accessToken":"linux-custom-tok","subscriptionType":"pro"}}"#,
        );
        let (creds, source) = claude_credentials_with(
            false,
            Some(custom_dir.to_str().unwrap()),
            tmp.path(),
            || Err("keychain should not run on Linux".into()),
        )
        .unwrap();
        let (tok, plan) = claude_oauth_from_creds(&creds, 1_700_000_000_000, source).unwrap();
        assert_eq!(tok, "linux-custom-tok");
        assert_eq!(plan.as_deref(), Some("pro"));
    }

    #[test]
    fn claude_macos_uses_keychain_by_default_and_honors_config_dir_file() {
        let tmp = TempDir::new("claude-macos");
        let (creds, source) = claude_credentials_with(true, None, tmp.path(), || {
            Ok(json!({"claudeAiOauth":{"accessToken":"mac-kc-tok","subscriptionType":"max"}}))
        })
        .unwrap();
        assert_eq!(source, "keychain entry");
        let (tok, plan) = claude_oauth_from_creds(&creds, 1_700_000_000_000, source).unwrap();
        assert_eq!(tok, "mac-kc-tok");
        assert_eq!(plan.as_deref(), Some("max"));

        let custom_dir = tmp.path().join("custom-claude");
        std::fs::create_dir_all(&custom_dir).unwrap();
        write_private(
            &custom_dir.join(".credentials.json"),
            r#"{"claudeAiOauth":{"accessToken":"mac-file-tok","subscriptionType":"pro"}}"#,
        );
        let (creds, source) = claude_credentials_with(
            true,
            Some(custom_dir.to_str().unwrap()),
            tmp.path(),
            || Err("keychain should not be called when CLAUDE_CONFIG_DIR has credentials".into()),
        )
        .unwrap();
        assert_eq!(source, "credentials file");
        let (tok, _) = claude_oauth_from_creds(&creds, 1_700_000_000_000, source).unwrap();
        assert_eq!(tok, "mac-file-tok");
    }

    #[test]
    fn claude_credentials_error_cases() {
        let tmp = TempDir::new("claude-errors");
        let missing = claude_credentials_with(false, None, tmp.path(), || unreachable!()).unwrap_err();
        assert_eq!(missing.message, "no Claude Code login (~/.claude/.credentials.json)");
        assert!(!missing.signed_in);

        let no_token = claude_oauth_from_creds(&json!({"claudeAiOauth":{}}), 1000, "keychain entry").unwrap_err();
        assert_eq!(no_token.message, "keychain entry has no access token");
        assert!(!no_token.signed_in);

        let expired = claude_oauth_from_creds(
            &json!({"claudeAiOauth":{"accessToken":"tok","expiresAt":1000}}),
            1000,
            "credentials file",
        )
        .unwrap_err();
        assert_eq!(expired.message, "access token expired; run `claude` once to refresh it");
        assert!(expired.signed_in, "an expired token is a real failure, not a missing sign-in");
    }

    #[test]
    fn codex_reads_default_and_codex_home() {
        let tmp = TempDir::new("codex");
        let default_dir = codex_home_from(None, tmp.path());
        assert_eq!(default_dir, tmp.path().join(".codex"));
        let missing = codex_auth_from_dir(&default_dir).err().unwrap();
        assert_eq!(missing.message, "no Codex login (~/.codex/auth.json)");
        assert!(!missing.signed_in);

        let custom = tmp.path().join("custom-codex");
        std::fs::create_dir_all(&custom).unwrap();
        write_private(
            &custom.join("auth.json"),
            r#"{"tokens":{"access_token":"codex-tok","account_id":"acct-42"}}"#,
        );
        let resolved = codex_home_from(Some(custom.to_str().unwrap()), tmp.path());
        let auth = codex_auth_from_dir(&resolved).ok().unwrap();
        assert_eq!(auth.token, "codex-tok");
        assert_eq!(auth.account_id.as_deref(), Some("acct-42"));
        assert_eq!(auth.account, None, "no id token, no account");
    }

    /// An unsigned JWT with these claims.
    fn jwt(claims: Value) -> String {
        let b64 = |bytes: &[u8]| {
            const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
            let mut out = String::new();
            for chunk in bytes.chunks(3) {
                let n = chunk.iter().enumerate().fold(0u32, |n, (i, &b)| n | u32::from(b) << (16 - 8 * i));
                for i in 0..=chunk.len() {
                    out.push(A[(n >> (18 - 6 * i) & 63) as usize] as char);
                }
            }
            out
        };
        format!("{}.{}.sig", b64(br#"{"alg":"none"}"#), b64(claims.to_string().as_bytes()))
    }

    #[test]
    fn codex_user_prefers_chatgpt_user_id_then_user_id_then_sub() {
        let auth = "https://api.openai.com/auth";
        let full = jwt(json!({"sub": "s-1", auth: {"chatgpt_user_id": "cu-1", "user_id": "u-1"}}));
        assert_eq!(codex_user_from_id_token(&full).as_deref(), Some("cu-1"));
        let user = jwt(json!({"sub": "s-1", auth: {"user_id": "u-1"}}));
        assert_eq!(codex_user_from_id_token(&user).as_deref(), Some("u-1"));
        assert_eq!(codex_user_from_id_token(&jwt(json!({"sub": "s-1"}))).as_deref(), Some("s-1"));
        assert_eq!(codex_user_from_id_token("not-a-jwt"), None);
        assert_eq!(base64url_decode("aGk_-w").as_deref(), Some(&[b'h', b'i', 0x3f, 0xfb][..]));
    }

    #[test]
    fn codex_account_combines_user_and_workspace() {
        let tmp = TempDir::new("codex-account");
        let id = jwt(json!({"https://api.openai.com/auth": {"chatgpt_user_id": "cu-1"}}));
        let write = |acct: &str| {
            let auth = json!({"tokens": {"access_token": "t", "account_id": acct, "id_token": id}});
            write_private(&tmp.path().join("auth.json"), &auth.to_string());
            codex_auth_from_dir(tmp.path()).ok().unwrap().account.unwrap()
        };
        let (a, b) = (write("ws-1"), write("ws-2"));
        assert_eq!(a, fingerprint("codex", "cu-1:ws-1"));
        assert_ne!(a, b);
    }

    #[test]
    fn account_ids_for_claude_antigravity_and_zed() {
        let claude = json!({"oauthAccount": {"accountUuid": "acc-1", "organizationUuid": "org-1"}});
        assert_eq!(claude_account_from(&claude), Some(fingerprint("claude", "acc-1:org-1")));
        assert_eq!(claude_account_from(&json!({})), None);
        assert_eq!(
            claude_account_file(Some("/cfg"), Path::new("/home/u")),
            PathBuf::from("/cfg/.claude.json")
        );
        assert_eq!(claude_account_file(None, Path::new("/home/u")), PathBuf::from("/home/u/.claude.json"));

        assert_eq!(antigravity_account_from(&json!({"id": "123"})), Some(fingerprint("antigravity", "123")));
        assert_eq!(antigravity_account_from(&json!({})), None);

        let billing = json!({"result": {"subscriptionStartTime": "2026-01-02T03:04:05+00:00"}});
        let by_start = fingerprint("chatllm", "subscription:2026-01-02T03:04:05+00:00");
        assert_eq!(abacus_account_from(&billing, "key-a"), by_start);
        assert_eq!(abacus_account_from(&billing, "key-b"), by_start, "one account, two keys");
        assert_eq!(abacus_account_from(&Value::Null, "key-a"), fingerprint("chatllm", "key:key-a"));

        let sub = json!({"subscription": {"id": "sub-1"}});
        assert_eq!(zed_account_from(&sub, "cookie"), fingerprint("zed", "sub-1"));
        assert_eq!(zed_account_from(&json!({}), "cookie"), fingerprint("zed", "session:cookie"));
    }

    #[test]
    fn t3_userdata_and_antigravity_lookup_respect_t3code_home() {
        let tmp = TempDir::new("t3-userdata");
        let personal = tmp.path().join(".t3-personal/userdata");
        std::fs::create_dir_all(&personal).unwrap();
        assert_eq!(t3_userdata_from(None, None, tmp.path()).as_deref(), Some(personal.as_path()));

        let t3_home = tmp.path().join("custom-t3-home");
        let expected_ud = t3_home.join("userdata");
        assert_eq!(
            t3_userdata_from(None, Some(t3_home.to_str().unwrap()), tmp.path()).as_deref(),
            Some(expected_ud.as_path())
        );

        let explicit_ud = tmp.path().join("explicit-userdata");
        assert_eq!(
            t3_userdata_from(
                Some(explicit_ud.to_str().unwrap()),
                Some(t3_home.to_str().unwrap()),
                tmp.path()
            )
            .as_deref(),
            Some(explicit_ud.as_path())
        );

        let ag_file = expected_ud.join("providers/antigravity/inst-1/antigravity-acp/acp_token.json");
        std::fs::create_dir_all(ag_file.parent().unwrap()).unwrap();
        write_private(&ag_file, r#"{"client_id":"cid"}"#);
        assert_eq!(find_antigravity_token_in(&expected_ud).unwrap().as_deref(), Some(ag_file.as_path()));
        assert_eq!(find_antigravity_token_in(tmp.path()), Ok(None), "no provider dir: not signed in");

        let secret_file = expected_ud.join("secrets/test.bin");
        std::fs::create_dir_all(secret_file.parent().unwrap()).unwrap();
        write_private(&secret_file, "  secret-val \n");
        assert_eq!(t3_secret_in(&expected_ud, "test.bin").unwrap().as_deref(), Some("secret-val"));
        assert_eq!(t3_secret_in(&expected_ud, "missing.bin"), Ok(None));
        // A secret path that cannot be read (here a directory) is an error, not a missing sign-in.
        std::fs::create_dir_all(expected_ud.join("secrets/dir.bin")).unwrap();
        assert!(t3_secret_in(&expected_ud, "dir.bin").is_err());
    }

    #[test]
    fn only_a_missing_credential_means_not_signed_in() {
        let tmp = TempDir::new("cred-errors");
        let missing = read_credential(&tmp.path().join("nope.json"), "no login").unwrap_err();
        assert_eq!((missing.message.as_str(), missing.signed_in), ("no login", false));
        // Exists but unreadable (a directory stands in for a permission error).
        let unreadable = read_credential(tmp.path(), "no login").unwrap_err();
        assert!(unreadable.signed_in && unreadable.message.starts_with("could not read"), "{unreadable:?}");
        // Codex: an unparsable auth.json is a real failure too.
        write_private(&tmp.path().join("auth.json"), "{not json");
        assert!(codex_auth_from_dir(tmp.path()).err().unwrap().signed_in);

        assert!(!keychain_failure(Some(44)).signed_in, "item not found");
        assert!(keychain_failure(Some(51)).signed_in);
        assert!(keychain_failure(None).signed_in);
    }

    #[test]
    fn cache_dir_respects_os_and_xdg_cache_home() {
        let home = Path::new("/home/tvha");
        assert_eq!(
            cache_dir_for(true, Some("/custom/cache"), home),
            PathBuf::from("/home/tvha/Library/Caches/ai-usage")
        );
        assert_eq!(
            cache_dir_for(false, None, home),
            PathBuf::from("/home/tvha/.cache/ai-usage")
        );
        assert_eq!(
            cache_dir_for(false, Some("  "), home),
            PathBuf::from("/home/tvha/.cache/ai-usage")
        );
        assert_eq!(
            cache_dir_for(false, Some("/custom/cache"), home),
            PathBuf::from("/custom/cache/ai-usage")
        );
    }
}
