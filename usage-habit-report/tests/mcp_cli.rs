//! `usage-habit-report mcp`: spawn the binary, speak JSON-RPC over its stdio,
//! and compare tool results with the CLI's `--json` output on a fixture store.

use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

const ALPHA: &str = "11111111-1111-4111-8111-111111111111";
const CODEX_A: &str = "aaaaaaaa-0000-4000-8000-00000000000a";

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn command(tmp: &Path, args: &[&str]) -> Command {
    let mut c = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"));
    c.arg("--db")
        .arg(tmp.join("usage.db"))
        .args(args)
        .env("XDG_CONFIG_HOME", tmp.join("config"))
        .env("HOME", tmp) // keep the real harness and T3 folders out
        .env_remove("CODEX_HOME")
        .env("USAGE_HABIT_REPORT_OFFLINE", "1");
    c
}

/// CLI `--json` output as a value.
fn cli_json(tmp: &Path, args: &[&str]) -> Value {
    let out = command(tmp, args).output().unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}

fn t3_state(path: &Path) {
    let c = Connection::open(path).unwrap();
    c.execute_batch(
        r#"CREATE TABLE provider_session_runtime (
               thread_id TEXT PRIMARY KEY, provider_name TEXT NOT NULL,
               resume_cursor_json TEXT);
           CREATE TABLE projection_threads (
               thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
               title TEXT NOT NULL, worktree_path TEXT);
           CREATE TABLE projection_projects (
               project_id TEXT PRIMARY KEY, title TEXT NOT NULL,
               workspace_root TEXT NOT NULL);
           INSERT INTO projection_projects VALUES ('p1', 'Gamma', '/home/demo/gamma');"#,
    )
    .unwrap();
    c.execute(
        "INSERT INTO provider_session_runtime VALUES ('t3-demo-thread', 'codex', ?1)",
        params![format!(r#"{{"threadId":"{CODEX_A}"}}"#)],
    )
    .unwrap();
    c.execute(
        "INSERT INTO projection_threads VALUES ('t3-demo-thread', 'p1', 'Demo thread', NULL)",
        [],
    )
    .unwrap();
}

/// A store holding the fixtures, a T3 link on Codex session `a`, and a second
/// Claude session sharing the `11111111` prefix with `ALPHA`.
fn seeded() -> tempfile::TempDir {
    let tmp = tempfile::tempdir().unwrap();
    t3_state(&tmp.path().join("state.sqlite"));
    let f = fixtures();
    let out = command(
        tmp.path(),
        &[
            "scan",
            "--claude-dir",
            f.join("claude/projects").to_str().unwrap(),
            "--codex-dir",
            f.join("codex").to_str().unwrap(),
            "--antigravity-dir",
            tmp.path().join("no-antigravity").to_str().unwrap(),
            "--t3-state",
            tmp.path().join("state.sqlite").to_str().unwrap(),
        ],
    )
    .output()
    .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    Connection::open(tmp.path().join("usage.db"))
        .unwrap()
        .execute(
            "INSERT INTO sessions (tool, session_id, title) VALUES
             ('claude-code', '11111111-1111-4111-8111-1111111111ff', 'Twin')",
            [],
        )
        .unwrap();
    tmp
}

struct Client {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: BufReader<ChildStdout>,
    next_id: i64,
}

impl Client {
    fn spawn(tmp: &Path, extra: &[&str]) -> Client {
        let mut args = extra.to_vec();
        args.push("mcp");
        let mut child = command(tmp, &args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        Client {
            child,
            stdin,
            stdout,
            next_id: 1,
        }
    }

    fn send_raw(&mut self, line: &str) {
        let s = self.stdin.as_mut().unwrap();
        writeln!(s, "{line}").unwrap();
        s.flush().unwrap();
    }

    fn read(&mut self) -> Value {
        let mut line = String::new();
        self.stdout.read_line(&mut line).unwrap();
        serde_json::from_str(&line).unwrap_or_else(|e| panic!("stdout not JSON ({e}): {line:?}"))
    }

    fn request(&mut self, method: &str, params: Value) -> Value {
        let id = self.next_id;
        self.next_id += 1;
        self.send_raw(
            &json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}).to_string(),
        );
        let reply = self.read();
        assert_eq!(reply["id"], json!(id));
        reply
    }

    fn initialize(&mut self) -> Value {
        let r = self.request(
            "initialize",
            json!({"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}),
        );
        self.send_raw(r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#);
        r
    }

    /// A tool call's `(is_error, parsed JSON text)`.
    fn call(&mut self, name: &str, arguments: Value) -> (bool, Value) {
        let r = self.request("tools/call", json!({"name": name, "arguments": arguments}));
        let res = &r["result"];
        assert_eq!(res["content"][0]["type"], "text", "{r}");
        let text = res["content"][0]["text"].as_str().unwrap();
        (
            res["isError"].as_bool().unwrap(),
            serde_json::from_str(text).unwrap(),
        )
    }

    fn finish(mut self) {
        drop(self.stdin.take());
        assert!(self.child.wait().unwrap().success());
    }
}

fn without_generated_at(mut v: Value) -> Value {
    if let Some(o) = v.as_object_mut() {
        o.remove("generated_at");
    }
    v
}

#[test]
fn initialize_negotiates_and_ping_works() {
    let tmp = seeded();
    let mut c = Client::spawn(tmp.path(), &["--no-scan"]);
    let r = c.initialize();
    assert_eq!(r["result"]["protocolVersion"], "2025-06-18");
    assert_eq!(r["result"]["serverInfo"]["name"], "usage-habit-report");
    assert!(r["result"]["capabilities"]["tools"].is_object());
    assert_eq!(c.request("ping", json!({}))["result"], json!({}));
    // An unknown requested version gets the newest handshake revision.
    let r = c.request("initialize", json!({"protocolVersion": "1999-01-01"}));
    assert_eq!(r["result"]["protocolVersion"], "2025-11-25");
    let r = c.request("no/such/method", json!({}));
    assert_eq!(r["error"]["code"], -32601);
    c.send_raw("not json");
    assert_eq!(c.read()["error"]["code"], -32700);
    c.finish();
}

#[test]
fn tools_list_has_every_tool_with_the_required_description() {
    let tmp = seeded();
    let mut c = Client::spawn(tmp.path(), &["--no-scan"]);
    c.initialize();
    let r = c.request("tools/list", json!({}));
    let tools = r["result"]["tools"].as_array().unwrap();
    let names: Vec<&str> = tools.iter().map(|t| t["name"].as_str().unwrap()).collect();
    assert_eq!(
        names,
        [
            "usage_summary",
            "usage_session",
            "usage_project",
            "usage_projects",
            "usage_models",
            "usage_agents",
            "usage_labels"
        ]
    );
    for t in tools {
        let d = t["description"].as_str().unwrap();
        assert!(d.contains("never contains prompt or response text"), "{d}");
        assert!(d.contains("estimates"), "{d}");
        assert!(d.contains("up to 30 s"), "{d}");
        assert_eq!(t["inputSchema"]["type"], "object");
    }
    c.finish();
}

#[test]
fn tool_results_equal_the_cli_json() {
    let tmp = seeded();
    let p = tmp.path();
    let mut c = Client::spawn(p, &["--no-scan"]);
    c.initialize();

    let cases: [(&str, Value, Vec<&str>); 8] = [
        (
            "usage_summary",
            json!({}),
            vec!["query", "summary", "--json"],
        ),
        (
            "usage_summary",
            json!({"group_by": "label", "since": "100000d"}),
            vec![
                "query",
                "summary",
                "--group-by",
                "label",
                "--since",
                "100000d",
                "--json",
            ],
        ),
        (
            "usage_session",
            json!({"key": ALPHA, "rows": true}),
            vec!["query", "session", ALPHA, "--rows", "--json"],
        ),
        (
            "usage_session",
            json!({"key": "t3-demo-thread"}),
            vec!["query", "session", "t3-demo-thread", "--json"],
        ),
        (
            "usage_projects",
            json!({}),
            vec!["query", "projects", "--json"],
        ),
        ("usage_models", json!({}), vec!["query", "models", "--json"]),
        ("usage_agents", json!({}), vec!["query", "agents", "--json"]),
        ("usage_labels", json!({}), vec!["query", "labels", "--json"]),
    ];
    for (tool, args, cli) in cases {
        let mut full = vec!["--no-scan"];
        full.extend(cli);
        let want = without_generated_at(cli_json(p, &full));
        let (is_error, got) = c.call(tool, args);
        assert!(!is_error, "{tool}: {got}");
        assert_eq!(without_generated_at(got), want, "{tool}");
    }
    // A project by folder name.
    let (is_error, got) = c.call("usage_project", json!({"key": "gamma"}));
    assert!(!is_error, "{got}");
    let want = cli_json(p, &["--no-scan", "query", "project", "gamma", "--json"]);
    assert_eq!(without_generated_at(got), without_generated_at(want));
    c.finish();
}

#[test]
fn ambiguous_and_missing_keys_are_error_results() {
    let tmp = seeded();
    let mut c = Client::spawn(tmp.path(), &["--no-scan"]);
    c.initialize();

    let (is_error, v) = c.call("usage_session", json!({"key": "11111111"}));
    assert!(is_error);
    assert_eq!(v["error"], "ambiguous");
    assert_eq!(v["key"], "11111111");
    let ids: Vec<&str> = v["candidates"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["session_id"].as_str().unwrap())
        .collect();
    assert_eq!(ids.len(), 2);
    assert!(ids.contains(&ALPHA));
    // Same body as the CLI prints for the same key.
    let cli = command(
        tmp.path(),
        &["--no-scan", "query", "session", "11111111", "--json"],
    )
    .output()
    .unwrap();
    assert_eq!(cli.status.code(), Some(2));
    assert_eq!(serde_json::from_slice::<Value>(&cli.stdout).unwrap(), v);

    let (is_error, v) = c.call("usage_session", json!({"key": "zzzz-nope"}));
    assert!(is_error);
    assert_eq!(v, json!({"error": "not found", "key": "zzzz-nope"}));
    let (is_error, v) = c.call("usage_project", json!({"key": "no-such-project"}));
    assert!(is_error);
    assert_eq!(v["error"], "not found");

    // Bad arguments are error results; an unknown tool is a protocol error.
    let (is_error, v) = c.call("usage_summary", json!({"since": "soon"}));
    assert!(
        is_error && v["error"].as_str().unwrap().contains("--since"),
        "{v}"
    );
    let (is_error, _) = c.call("usage_session", json!({}));
    assert!(is_error);
    let r = c.request("tools/call", json!({"name": "usage_nope", "arguments": {}}));
    assert_eq!(r["error"]["code"], -32602);
    c.finish();
}

#[test]
fn modern_requests_get_result_type_and_structured_content() {
    let tmp = seeded();
    let mut c = Client::spawn(tmp.path(), &["--no-scan"]);
    let meta = json!({"io.modelcontextprotocol/protocolVersion": "2026-07-28"});
    let r = c.request("server/discover", json!({"_meta": meta}));
    assert_eq!(r["result"]["supportedVersions"][0], "2026-07-28");
    let r = c.request(
        "tools/call",
        json!({"_meta": meta, "name": "usage_models", "arguments": {}}),
    );
    assert_eq!(r["result"]["resultType"], "complete");
    assert!(r["result"]["structuredContent"].is_array());
    let r = c.request(
        "tools/list",
        json!({"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}}),
    );
    assert_eq!(r["error"]["code"], -32022);
    c.finish();
}

fn copy_dir(from: &Path, to: &Path) {
    std::fs::create_dir_all(to).unwrap();
    for e in std::fs::read_dir(from).unwrap() {
        let e = e.unwrap();
        let dest = to.join(e.file_name());
        if e.file_type().unwrap().is_dir() {
            copy_dir(&e.path(), &dest);
        } else {
            std::fs::copy(e.path(), dest).unwrap();
        }
    }
}

/// HOME holds the Claude fixtures under `~/.claude/projects`, the store is empty.
fn summary_requests(extra: &[&str]) -> i64 {
    let tmp = tempfile::tempdir().unwrap();
    copy_dir(
        &fixtures().join("claude/projects"),
        &tmp.path().join(".claude/projects"),
    );
    let mut c = Client::spawn(tmp.path(), extra);
    c.initialize();
    let (is_error, v) = c.call("usage_summary", json!({}));
    assert!(!is_error, "{v}");
    c.finish();
    v["totals"]["requests"].as_i64().unwrap()
}

#[test]
fn first_call_runs_the_catch_up_unless_no_scan() {
    assert!(summary_requests(&[]) > 0);
    assert_eq!(summary_requests(&["--no-scan"]), 0);
}
