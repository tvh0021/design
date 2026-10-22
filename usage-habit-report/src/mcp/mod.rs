//! `usage-habit-report mcp`: a stdio MCP server over the same query functions
//! the CLI uses. JSON-RPC 2.0, one message per line on stdin and stdout; logs
//! go to stderr. No SDK: the protocol surface is `initialize`,
//! `notifications/initialized`, `ping`, `server/discover`, `tools/list` and
//! `tools/call`.
//!
//! Protocol versions: the handshake revisions `2025-11-25`, `2025-06-18`,
//! `2025-03-26` and `2024-11-05` (the client's version is echoed when known,
//! otherwise the newest is offered), plus the per-request-metadata revision
//! `2026-07-28` that the MCP site lists as current (`server/discover`, version
//! in each request's `_meta`).
//!
//! Catch-up rule: the first `tools/call` runs the caller-supplied catch-up
//! (scan, enrich, price), and so does any later call that arrives more than
//! [`CATCH_UP_EVERY`] after the previous one finished. `--no-scan` disables it.
//! Nothing runs at startup, so `initialize` answers at once.

use crate::{agents, labels, paths, project, query, rates, scan, store::Store};
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::io::{BufRead, Write};
use std::time::{Duration, Instant};

/// Handshake revisions first (newest first), then the per-request revision.
pub const SUPPORTED_VERSIONS: [&str; 5] = [
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
];
/// Newest handshake-era revision, offered to `initialize` callers that ask for something unknown.
const LEGACY_LATEST: &str = "2025-11-25";
const MODERN: &str = "2026-07-28";
const VERSION_KEY: &str = "io.modelcontextprotocol/protocolVersion";
/// A call this long after the last catch-up repeats it.
pub const CATCH_UP_EVERY: Duration = Duration::from_secs(60);

const NOTE: &str = " Output never contains prompt or response text (only session and T3 thread titles). Dollar figures are estimates at LiteLLM list prices. The first call may run a catch-up scan of the harness logs that can take up to 30 s on a cold store; later calls repeat it only when the previous one is over 60 s old.";

const SINCE_DESC: &str =
    "Only requests in the last N hours, days or weeks: 12h, 7d, 2w. Omit for all time.";

/// A catch-up step supplied by the binary (scan, enrich, price).
pub type CatchUp<'a> = Box<dyn FnMut(&mut Store) -> Result<()> + 'a>;

/// What a tool produced: the CLI's JSON value, and whether it is an error body.
struct Outcome {
    value: Value,
    is_error: bool,
}

impl Outcome {
    fn ok(value: Value) -> Self {
        Outcome {
            value,
            is_error: false,
        }
    }
    fn err(value: Value) -> Self {
        Outcome {
            value,
            is_error: true,
        }
    }
}

pub struct Server<'a> {
    store: Store,
    offline: bool,
    no_scan: bool,
    catch_up: CatchUp<'a>,
    last_catch_up: Option<Instant>,
}

/// Run the server on stdin and stdout until stdin closes.
pub fn serve(store: Store, offline: bool, no_scan: bool, catch_up: CatchUp<'_>) -> Result<()> {
    let mut server = Server::new(store, offline, no_scan, catch_up);
    eprintln!("usage-habit-report mcp: ready on stdio");
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        if let Some(reply) = server.handle_line(&line) {
            let mut out = stdout.lock();
            writeln!(out, "{reply}")?;
            out.flush()?;
        }
    }
    eprintln!("usage-habit-report mcp: stdin closed, exiting");
    Ok(())
}

fn rpc_error(id: &Value, code: i64, message: &str, data: Option<Value>) -> Value {
    let mut e = json!({"code": code, "message": message});
    if let Some(d) = data {
        e["data"] = d;
    }
    json!({"jsonrpc": "2.0", "id": id, "error": e})
}

fn text_arg<'v>(args: &'v Value, name: &str) -> Result<Option<&'v str>> {
    match args.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => Ok(Some(s)),
        Some(_) => bail!("argument {name:?} must be a string"),
    }
}

fn bool_arg(args: &Value, name: &str) -> Result<bool> {
    match args.get(name) {
        None | Some(Value::Null) => Ok(false),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => bail!("argument {name:?} must be a boolean"),
    }
}

fn key_arg(args: &Value) -> Result<&str> {
    match text_arg(args, "key")? {
        Some(k) if !k.trim().is_empty() => Ok(k),
        _ => bail!("argument \"key\" is required"),
    }
}

fn since_arg(args: &Value, now: i64) -> Result<Option<i64>> {
    Ok(text_arg(args, "since")?
        .map(query::parse_since)
        .transpose()?
        .map(|w| now - w))
}

impl<'a> Server<'a> {
    pub fn new(store: Store, offline: bool, no_scan: bool, catch_up: CatchUp<'a>) -> Self {
        Server {
            store,
            offline,
            no_scan,
            catch_up,
            last_catch_up: None,
        }
    }

    /// Handle one input line; `None` for notifications.
    pub fn handle_line(&mut self, line: &str) -> Option<String> {
        let msg: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(e) => {
                return Some(
                    rpc_error(&Value::Null, -32700, &format!("parse error: {e}"), None).to_string(),
                );
            }
        };
        if !msg.is_object() {
            return Some(
                rpc_error(
                    &Value::Null,
                    -32600,
                    "expected a single JSON-RPC object",
                    None,
                )
                .to_string(),
            );
        }
        let id = msg.get("id").cloned();
        let Some(method) = msg.get("method").and_then(Value::as_str) else {
            // A response to something we never asked: ignore.
            return id.map(|id| rpc_error(&id, -32600, "missing method", None).to_string());
        };
        let params = msg.get("params").cloned().unwrap_or(Value::Null);
        let reply = self.dispatch(method, &params);
        let id = id?; // notifications get no reply
        Some(
            match reply {
                Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
                Err((code, message, data)) => rpc_error(&id, code, &message, data),
            }
            .to_string(),
        )
    }

    fn dispatch(
        &mut self,
        method: &str,
        params: &Value,
    ) -> Result<Value, (i64, String, Option<Value>)> {
        let requested = params
            .pointer("/_meta")
            .and_then(|m| m.get(VERSION_KEY))
            .and_then(Value::as_str);
        if let Some(v) = requested
            && !SUPPORTED_VERSIONS.contains(&v)
        {
            return Err((
                -32022,
                "Unsupported protocol version".into(),
                Some(json!({"supported": SUPPORTED_VERSIONS, "requested": v})),
            ));
        }
        let modern = requested == Some(MODERN);
        let mut result = match method {
            "initialize" => {
                let asked = params.get("protocolVersion").and_then(Value::as_str);
                let version = match asked {
                    Some(v) if SUPPORTED_VERSIONS[1..].contains(&v) => v,
                    _ => LEGACY_LATEST,
                };
                json!({
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": false}},
                    "serverInfo": server_info(),
                    "instructions": "Local token-usage statistics for Claude Code, Codex and Antigravity. Read-only; no prompt text is ever returned."
                })
            }
            "server/discover" => json!({
                "resultType": "complete",
                "supportedVersions": SUPPORTED_VERSIONS,
                "capabilities": {"tools": {}},
                "_meta": {"io.modelcontextprotocol/serverInfo": server_info()},
                "instructions": "Local token-usage statistics for Claude Code, Codex and Antigravity. Read-only; no prompt text is ever returned."
            }),
            "ping" => json!({}),
            "tools/list" => json!({"tools": tool_definitions()}),
            "tools/call" => self.tools_call(params, modern)?,
            "notifications/initialized" | "notifications/cancelled" => Value::Null,
            m if m.starts_with("notifications/") => Value::Null,
            m => return Err((-32601, format!("method not found: {m}"), None)),
        };
        if modern && method != "server/discover" && result.is_object() {
            result["resultType"] = json!("complete");
        }
        Ok(result)
    }

    fn tools_call(
        &mut self,
        params: &Value,
        modern: bool,
    ) -> Result<Value, (i64, String, Option<Value>)> {
        let Some(name) = params.get("name").and_then(Value::as_str) else {
            return Err((-32602, "tools/call needs a tool name".into(), None));
        };
        if !tool_definitions()
            .iter()
            .any(|t| t["name"].as_str() == Some(name))
        {
            return Err((-32602, format!("unknown tool: {name}"), None));
        }
        let args = params.get("arguments").cloned().unwrap_or(json!({}));
        if !args.is_object() {
            return Err((-32602, "arguments must be an object".into(), None));
        }
        let started = Instant::now();
        let outcome = match self.run_tool(name, &args) {
            Ok(o) => o,
            Err(e) => Outcome::err(json!({"error": format!("{e:#}")})),
        };
        eprintln!(
            "tools/call {name}: {} in {:.3} s",
            if outcome.is_error { "error" } else { "ok" },
            started.elapsed().as_secs_f64()
        );
        let mut result = json!({
            "content": [{"type": "text", "text": outcome.value.to_string()}],
            "isError": outcome.is_error,
        });
        if modern {
            result["structuredContent"] = outcome.value;
        }
        Ok(result)
    }

    /// Catch up unless disabled, never started, or the last run is over a minute old.
    fn ensure_fresh(&mut self) -> Result<()> {
        if self.no_scan {
            return Ok(());
        }
        if self
            .last_catch_up
            .is_some_and(|t| t.elapsed() < CATCH_UP_EVERY)
        {
            return Ok(());
        }
        (self.catch_up)(&mut self.store)?;
        self.last_catch_up = Some(Instant::now());
        Ok(())
    }

    /// The CLI's `priced` step: load rates, price rows that lack a cost.
    fn priced(&mut self) -> Result<rates::Rates> {
        let path = paths::config_dir()?.join("rates.json");
        let r = rates::load(&path, self.offline, scan::now_ms(), rates::fetch_litellm);
        let all = r.origin == rates::Origin::Fetched;
        rates::price_requests(&mut self.store.conn, &r.file, all)?;
        Ok(r)
    }

    fn run_tool(&mut self, name: &str, args: &Value) -> Result<Outcome> {
        // Validate arguments before any scan so a typo fails fast.
        let now = scan::now_ms();
        let since_ms = if matches!(name, "usage_models" | "usage_labels" | "usage_value") {
            None
        } else {
            since_arg(args, now)?
        };
        match name {
            "usage_summary" => {
                let group_by = text_arg(args, "group_by")?.unwrap_or("tool-model");
                if !["tool-model", "label"].contains(&group_by) {
                    bail!("group_by must be \"tool-model\" or \"label\"");
                }
                self.ensure_fresh()?;
                let r = self.priced()?;
                let now = scan::now_ms();
                let v = if group_by == "label" {
                    let mut s = labels::summary_by_label(&self.store.conn, since_ms, now)?;
                    s.rates_fetched_at = Some(r.file.fetched_at);
                    serde_json::to_value(&s)?
                } else {
                    let mut s = query::summary(&self.store.conn, since_ms, now)?;
                    s.rates_fetched_at = Some(r.file.fetched_at);
                    serde_json::to_value(&s)?
                };
                Ok(Outcome::ok(v))
            }
            "usage_session" => {
                let key = key_arg(args)?;
                let rows = bool_arg(args, "rows")?;
                self.ensure_fresh()?;
                let now = scan::now_ms();
                let found = match query::resolve_session(&self.store.conn, key)? {
                    query::SessionLookup::Found(f) => f,
                    query::SessionLookup::NotFound => {
                        return Ok(Outcome::err(json!({"error": "not found", "key": key})));
                    }
                    query::SessionLookup::Ambiguous(refs) => {
                        let c = query::session_candidates(&self.store.conn, &refs)?;
                        return Ok(Outcome::err(
                            json!({"error": "ambiguous", "key": key, "candidates": c}),
                        ));
                    }
                };
                let mut report =
                    query::session_report(&self.store.conn, &found, since_ms, rows, now)?;
                let path = paths::config_dir()?.join("rates.json");
                let r = rates::load(&path, self.offline, scan::now_ms(), rates::fetch_litellm);
                report.rates_fetched_at = Some(r.file.fetched_at);
                Ok(Outcome::ok(serde_json::to_value(&report)?))
            }
            "usage_project" => {
                let key = key_arg(args)?;
                self.ensure_fresh()?;
                let now = scan::now_ms();
                let found = match project::lookup(&self.store.conn, key)? {
                    project::Lookup::Found(p) => p,
                    project::Lookup::NotFound => {
                        return Ok(Outcome::err(json!({"error": "not found", "key": key})));
                    }
                    project::Lookup::Ambiguous(c) => {
                        return Ok(Outcome::err(
                            json!({"error": "ambiguous", "key": key, "candidates": c}),
                        ));
                    }
                };
                let r = self.priced()?;
                let mut d = project::detail(&self.store.conn, &found, since_ms, now)?;
                d.rates_fetched_at = Some(r.file.fetched_at);
                Ok(Outcome::ok(serde_json::to_value(&d)?))
            }
            "usage_projects" => {
                self.ensure_fresh()?;
                let r = self.priced()?;
                let mut l = project::list(&self.store.conn, since_ms, scan::now_ms())?;
                l.rates_fetched_at = Some(r.file.fetched_at);
                Ok(Outcome::ok(serde_json::to_value(&l)?))
            }
            "usage_models" => {
                self.ensure_fresh()?;
                let r = self.priced()?;
                let rows = rates::model_rows(&self.store.conn, &r.file)?;
                Ok(Outcome::ok(serde_json::to_value(&rows)?))
            }
            "usage_agents" => {
                self.ensure_fresh()?;
                let report = agents::agents_report(&self.store.conn, since_ms, scan::now_ms())?;
                Ok(Outcome::ok(serde_json::to_value(&report)?))
            }
            "usage_labels" => {
                self.ensure_fresh()?;
                self.priced()?;
                let report = labels::label_report(&self.store.conn, scan::now_ms())?;
                Ok(Outcome::ok(serde_json::to_value(&report)?))
            }
            "usage_value" => {
                self.ensure_fresh()?;
                self.priced()?;
                let subscriptions = crate::config::load()?.subscriptions;
                let report = crate::value::report(
                    &self.store.conn,
                    &subscriptions,
                    scan::now_ms(),
                    &chrono::Local,
                )?;
                Ok(Outcome::ok(serde_json::to_value(&report)?))
            }
            other => bail!("unknown tool: {other}"),
        }
    }
}

fn server_info() -> Value {
    json!({"name": "usage-habit-report", "version": env!("CARGO_PKG_VERSION")})
}

fn tool(name: &str, title: &str, what: &str, props: Value, required: &[&str]) -> Value {
    json!({
        "name": name,
        "title": title,
        "description": format!("{what}{NOTE}"),
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": false,
        },
        "annotations": {"readOnlyHint": true, "idempotentHint": true},
    })
}

/// The tool list, in a fixed order.
pub fn tool_definitions() -> Vec<Value> {
    let since = json!({"type": "string", "description": SINCE_DESC});
    let key = |what: &str| json!({"type": "string", "description": what});
    vec![
        tool(
            "usage_summary",
            "Token usage summary",
            "Token totals and estimated cost across Claude Code, Codex and Antigravity, grouped by tool and model (default) or by task label. Same JSON as `usage-habit-report query summary --json`.",
            json!({
                "since": since,
                "group_by": {
                    "type": "string",
                    "enum": ["tool-model", "label"],
                    "description": "How to group the totals. Default tool-model."
                }
            }),
            &[],
        ),
        tool(
            "usage_session",
            "One session's usage",
            "One session: tokens, models, estimated cost, subagent fan-out, optionally one row per request. The key is a session id, a unique session-id prefix, or a T3 thread id. An ambiguous key returns an error result with a `candidates` list; an unknown key returns an error result. Same JSON as `usage-habit-report query session --json`.",
            json!({
                "key": key("Session id, unique prefix of one, or T3 thread id."),
                "rows": {"type": "boolean", "description": "Add one row per request (time, model, effort, tokens, cost). Default false."},
                "since": since,
            }),
            &["key"],
        ),
        tool(
            "usage_project",
            "One project's usage",
            "One project's totals and per-tool breakdown. The key is a project path (or any directory inside it) or a folder name. Several projects with the same folder name return an error result with a `candidates` list; no match returns an error result. Same JSON as `usage-habit-report query project --json`.",
            json!({
                "key": key("Project path, a directory inside it, or folder name."),
                "since": since,
            }),
            &["key"],
        ),
        tool(
            "usage_projects",
            "All projects' usage",
            "Every project with its totals, session count and first and last request time. Same JSON as `usage-habit-report query projects --json`.",
            json!({"since": since}),
            &[],
        ),
        tool(
            "usage_models",
            "Model names and price coverage",
            "Raw model names with their canonical name, LiteLLM key, and whether they have a price; unpriced rows first. Returns a bare array. Same JSON as `usage-habit-report query models --json`.",
            json!({}),
            &[],
        ),
        tool(
            "usage_agents",
            "Subagent usage habits",
            "Subagent fan-out histogram, model mix, effort mix, subagent token share, and the heaviest fan-outs. Same JSON as `usage-habit-report query agents --json`.",
            json!({"since": since}),
            &[],
        ),
        tool(
            "usage_labels",
            "Usage per task label",
            "Sessions and tokens per task label (research, coding, debugging, writing, review, data-analysis, ops-config, other). Same JSON as `usage-habit-report query labels --json`.",
            json!({}),
            &[],
        ),
        tool(
            "usage_value",
            "Subscription value",
            "What each subscription buys: tokens, requests, sessions and estimated API cost per full session (5 h) and weekly limit window, extrapolated from recorded limit readings (Codex) or rejections (Claude Code), with per-month drift; plus each month's estimated API cost against the plan price. Same JSON as `usage-habit-report query value --json`.",
            json!({}),
            &[],
        ),
    ]
}
