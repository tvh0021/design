//! `serve` listens on 127.0.0.1 only, prints its URL first, answers `/` with
//! the dashboard, and 404s anything else.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::Path;
use std::process::{Child, Command, Stdio};

struct Server {
    child: Child,
    port: u16,
    /// Kept open so the server's request log has somewhere to go.
    _log: BufReader<std::process::ChildStdout>,
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn start(tmp: &Path) -> Server {
    let mut child = Command::new(env!("CARGO_BIN_EXE_usage-habit-report"))
        .arg("--db")
        .arg(tmp.join("usage.db"))
        .args(["serve", "--port", "0", "--no-open"])
        .env("XDG_CONFIG_HOME", tmp.join("config"))
        .env("HOME", tmp)
        .env("USAGE_HABIT_REPORT_OFFLINE", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .unwrap();
    let mut first = String::new();
    let mut log = BufReader::new(child.stdout.take().unwrap());
    log.read_line(&mut first).unwrap();
    let url = first
        .strip_prefix("serving http://127.0.0.1:")
        .unwrap_or_else(|| panic!("unexpected first line {first:?}"));
    let port: u16 = url.split('/').next().unwrap().parse().unwrap();
    assert_ne!(port, 0);
    Server {
        child,
        port,
        _log: log,
    }
}

fn get(port: u16, path: &str) -> (u16, String) {
    let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
    write!(s, "GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").unwrap();
    let mut raw = String::new();
    s.read_to_string(&mut raw).unwrap();
    let status: u16 = raw.split_whitespace().nth(1).unwrap().parse().unwrap();
    let body = raw.split_once("\r\n\r\n").unwrap().1.to_string();
    (status, body)
}

#[test]
fn serves_the_dashboard_on_loopback() {
    let tmp = tempfile::tempdir().unwrap();
    // Missing store: the page says what to run.
    let server = start(tmp.path());
    let (status, body) = get(server.port, "/");
    assert_eq!(status, 200);
    assert!(body.contains("Run <code>usage-habit-report scan</code>"));
    drop(server);

    // A store with one request: the map renders and reconciles.
    let store = usage_habit_report::store::Store::open(&tmp.path().join("usage.db")).unwrap();
    store
        .conn
        .execute_batch(
            "INSERT INTO sessions (tool, session_id) VALUES ('codex', 's1');
             INSERT INTO requests (tool, request_key, session_id, ts_ms, input_tokens, output_tokens)
                 VALUES ('codex', 'r1', 's1', 1792380600000, 100, 50);",
        )
        .unwrap();
    drop(store);
    let server = start(tmp.path());
    let (status, body) = get(server.port, "/?window=all&tool=codex");
    assert_eq!(status, 200);
    assert!(body.starts_with("<!doctype html>"));
    assert!(body.contains("<table id=\"map\""));
    assert!(
        body.contains("<tr class=\"total\"><th scope=\"row\">Total</th><td>1</td><td>100</td>")
    );
    assert!(!body.contains("http://") && !body.contains("https://"));
    let (status, _) = get(server.port, "/assets/anything");
    assert_eq!(status, 404);
    // The store is untouched: serve opens it read-only and never migrates or prices.
    let (status, _) = get(server.port, "/");
    assert_eq!(status, 200);
}
