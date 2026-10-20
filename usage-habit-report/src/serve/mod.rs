//! `usage-habit-report serve`: a loopback-only HTTP server that renders the
//! dashboard from the store. The store is opened read-only for every
//! request, so the page always shows the latest scan and never writes.
//!
//! Layout: `data` runs the SQL, `routes` turns a request into a page model,
//! `html` turns the model into a document, and the CSS and JS under
//! `assets/` are embedded at compile time. Nothing is fetched from the
//! network by the server or the page.

pub mod data;
pub mod html;
pub mod routes;

use crate::{paths, rates, scan};
use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags};
use std::io::Write;
use std::path::Path;
use tiny_http::{Header, Response, Server, StatusCode};

/// Only ever this address. There is no option to bind elsewhere.
const BIND_HOST: &str = "127.0.0.1";

pub struct Options {
    /// 0 asks the OS for a free port; the chosen port is printed either way.
    pub port: u16,
    pub open_browser: bool,
}

/// Open the store read-only, or None when the file does not exist yet.
fn open_read_only(db: &Path) -> Result<Option<Connection>> {
    if !db.exists() {
        return Ok(None);
    }
    let conn = Connection::open_with_flags(
        db,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("opening {} read-only", db.display()))?;
    conn.busy_timeout(std::time::Duration::from_secs(2))?;
    // Every panel scans `requests`; a page cache that holds the whole store
    // (64 MiB) lets the later scans hit memory instead of re-reading pages.
    // Connection-local and read-only: nothing is written.
    conn.execute_batch("PRAGMA cache_size = -65536;")?;
    Ok(Some(conn))
}

/// When the rates file on disk was fetched, for the footer. Never fetches.
fn rates_fetched_at() -> Option<String> {
    let path = paths::config_dir().ok()?.join("rates.json");
    let r = rates::load(&path, true, scan::now_ms(), || {
        anyhow::bail!("serve never fetches rates")
    });
    match r.origin {
        rates::Origin::File => Some(r.file.fetched_at.clone()),
        _ => None,
    }
}

struct Reply {
    status: u16,
    content_type: &'static str,
    body: String,
}

fn reply(status: u16, content_type: &'static str, body: String) -> Reply {
    Reply {
        status,
        content_type,
        body,
    }
}

/// Route one request. Only `/` (with an optional query) serves the page.
fn handle(db: &Path, url: &str, rates_at: Option<&str>) -> Reply {
    let (path, query) = url.split_once('?').unwrap_or((url, ""));
    if path != "/" {
        return reply(404, "text/plain; charset=utf-8", "not found\n".into());
    }
    let conn = match open_read_only(db) {
        Ok(Some(c)) => c,
        Ok(None) => {
            return reply(
                200,
                "text/html; charset=utf-8",
                html::render_missing(&db.display().to_string()),
            );
        }
        Err(e) => {
            return reply(500, "text/plain; charset=utf-8", format!("{e:#}\n"));
        }
    };
    match routes::render_dashboard(&conn, query, scan::now_ms(), rates_at.map(String::from)) {
        Ok(page) => reply(200, "text/html; charset=utf-8", page),
        Err(e) => reply(500, "text/plain; charset=utf-8", format!("{e:#}\n")),
    }
}

fn open_in_browser(url: &str) {
    #[cfg(target_os = "macos")]
    let cmd = "open";
    #[cfg(not(target_os = "macos"))]
    let cmd = "xdg-open";
    if let Err(e) = std::process::Command::new(cmd)
        .arg(url)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
    {
        eprintln!("could not open a browser ({e}); open {url} yourself");
    }
}

/// Serve until the process is stopped. Prints the URL as the first line.
pub fn run(db: &Path, opts: Options) -> Result<()> {
    let server = Server::http((BIND_HOST, opts.port))
        .map_err(|e| anyhow::anyhow!("binding {BIND_HOST}:{}: {e}", opts.port))?;
    let addr = server
        .server_addr()
        .to_ip()
        .context("server has no IP address")?;
    let url = format!("http://{}:{}/", addr.ip(), addr.port());
    println!("serving {url} (stop with Ctrl-C)");
    std::io::stdout().flush()?;
    if opts.open_browser {
        open_in_browser(&url);
    }
    let rates_at = rates_fetched_at();
    for request in server.incoming_requests() {
        let started = std::time::Instant::now();
        let url = request.url().to_string();
        let r = handle(db, &url, rates_at.as_deref());
        let mut response = Response::from_string(r.body).with_status_code(StatusCode(r.status));
        if let Ok(h) = Header::from_bytes("Content-Type", r.content_type) {
            response = response.with_header(h);
        }
        if let Ok(h) = Header::from_bytes("Cache-Control", "no-store") {
            response = response.with_header(h);
        }
        let _ = request.respond(response);
        // A closed stdout (the terminal went away) must not stop the server.
        let _ = writeln!(
            std::io::stdout(),
            "{} {} {:.1} ms",
            r.status,
            url,
            started.elapsed().as_secs_f64() * 1000.0
        );
    }
    Ok(())
}
