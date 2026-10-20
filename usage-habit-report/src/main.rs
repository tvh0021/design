use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use usage_habit_report::{
    agents, antigravity, codex, labels, mcp, paths, project, query, rates, scan, serve,
    store::Store, t3,
};

#[derive(Parser)]
#[command(
    name = "usage-habit-report",
    version,
    about = "Local token-usage store for AI coding harnesses"
)]
struct Cli {
    /// SQLite store path [default: ~/.local/share/usage-habit-report/usage.db]
    #[arg(long, global = true, env = "USAGE_HABIT_REPORT_DB")]
    db: Option<PathBuf>,
    /// Never fetch model rates; use rates.json or the built-in snapshot.
    #[arg(
        long,
        global = true,
        env = "USAGE_HABIT_REPORT_OFFLINE",
        value_parser = clap::builder::BoolishValueParser::new()
    )]
    offline: bool,
    /// Skip the catch-up that every `query` otherwise starts with (scan, enrich, price).
    #[arg(long, global = true)]
    no_scan: bool,
    #[command(subcommand)]
    command: Command,
}

/// Where `scan` and `rebuild` read from; each default is the harness's usual folder.
#[derive(clap::Args, Default)]
struct ScanArgs {
    /// Claude Code projects folder [default: ~/.claude/projects]
    #[arg(long)]
    claude_dir: Option<PathBuf>,
    /// Codex home; reads its sessions/ and archived_sessions/ [default: $CODEX_HOME or ~/.codex]
    #[arg(long)]
    codex_dir: Option<PathBuf>,
    /// Antigravity conversations folder; repeatable
    /// [default: ~/.gemini/antigravity/conversations and the T3 bridge folders]
    #[arg(long = "antigravity-dir")]
    antigravity_dirs: Vec<PathBuf>,
    /// T3 Code state database, read-only; repeatable; a missing path is skipped
    /// [default: ~/.t3/userdata/state.sqlite and ~/.t3-personal/userdata/state.sqlite]
    #[arg(long = "t3-state")]
    t3_states: Vec<PathBuf>,
}

#[derive(Subcommand)]
enum Command {
    /// Read new bytes from every harness log into the store.
    Scan {
        #[command(flatten)]
        args: ScanArgs,
    },
    /// Drop the store's usage rows (sources, sessions, requests, Codex cursors) and
    /// read every raw log again. Hand-set labels and rates.json are kept. Usage whose
    /// raw logs have since been deleted is lost.
    Rebuild {
        #[command(flatten)]
        args: ScanArgs,
    },
    /// Run a stdio MCP server exposing the query commands as tools.
    /// Registration snippets are in README.md. The catch-up runs on the first
    /// tool call, and again when a call comes more than 60 s after the last;
    /// `--no-scan` turns it off.
    Mcp,
    /// Read from the store.
    Query {
        #[command(subcommand)]
        what: QueryCommand,
    },
    /// Set a session's task label by hand (survives rescans and rebuilds).
    Label {
        /// Session id or a unique prefix of it.
        session: String,
        /// One of: research, coding, debugging, writing, review, data-analysis, ops-config, other.
        kind: String,
    },
    /// Model prices (LiteLLM) and request costs.
    Rates {
        #[command(subcommand)]
        what: RatesCommand,
    },
    /// Open the dashboard: a loopback-only web page rendered from the store.
    Serve {
        /// Port on 127.0.0.1; 0 picks a free one. The URL is printed.
        #[arg(long, default_value_t = 7878)]
        port: u16,
        /// Do not open the browser.
        #[arg(long)]
        no_open: bool,
    },
}

#[derive(Subcommand)]
enum RatesCommand {
    /// Fetch LiteLLM prices into rates.json, then re-price every request.
    Update,
    /// Price requests that have no cost yet (or every request with --all).
    Apply {
        #[arg(long)]
        all: bool,
    },
}

#[derive(Clone, Copy, clap::ValueEnum)]
enum GroupBy {
    /// Group by tool and model (default).
    ToolModel,
    /// Group by task label, then tool.
    Label,
}

#[derive(Subcommand)]
enum QueryCommand {
    /// Token totals grouped by tool and model, or by task label.
    Summary {
        /// How to group the totals.
        #[arg(long, value_enum, default_value = "tool-model")]
        group_by: GroupBy,
        /// Print JSON (shape documented in README.md).
        #[arg(long)]
        json: bool,
        /// Only requests in the last N hours/days/weeks: 12h, 7d, 2w.
        #[arg(long)]
        since: Option<String>,
    },
    /// One session: tokens, models, cost, subagents. Key is a session id, a T3 thread id,
    /// or a session-id prefix. Exit 1 when nothing matches, 2 when several sessions do.
    Session {
        /// Session id, T3 thread id, or unique prefix of a session id.
        key: String,
        /// Print JSON (shape documented in docs/json-shapes.md).
        #[arg(long)]
        json: bool,
        /// Add one row per request (time, model, effort, tokens, cost).
        #[arg(long)]
        rows: bool,
        /// Only requests in the last N hours/days/weeks: 12h, 7d, 2w.
        #[arg(long)]
        since: Option<String>,
    },
    /// Same as `query session`, but the key is matched only as a T3 thread id.
    Thread {
        /// T3 thread id.
        thread_id: String,
        /// Print JSON (shape documented in docs/json-shapes.md).
        #[arg(long)]
        json: bool,
        /// Add one row per request (time, model, effort, tokens, cost).
        #[arg(long)]
        rows: bool,
        /// Only requests in the last N hours/days/weeks: 12h, 7d, 2w.
        #[arg(long)]
        since: Option<String>,
    },
    /// Raw model names with their canonical name and whether they have a price.
    Models {
        /// Print JSON.
        #[arg(long)]
        json: bool,
    },
    /// One project's totals and per-tool breakdown, by path or folder name.
    /// Exit 1 when nothing matches, 2 when several folders share the name.
    Project {
        /// Project path (or any directory inside it), or folder name.
        key: String,
        /// Print JSON (shape documented in README.md).
        #[arg(long)]
        json: bool,
        /// Only requests in the last N hours/days/weeks: 12h, 7d, 2w.
        #[arg(long)]
        since: Option<String>,
    },
    /// Subagent fan-out, model mix, and token share.
    Agents {
        /// Print JSON (shape documented in README.md).
        #[arg(long)]
        json: bool,
        /// Only requests in the last N hours/days/weeks: 12h, 7d, 2w.
        #[arg(long)]
        since: Option<String>,
    },
    /// Every project with totals.
    Projects {
        /// Print JSON (shape documented in README.md).
        #[arg(long)]
        json: bool,
        /// Only requests in the last N hours/days/weeks: 12h, 7d, 2w.
        #[arg(long)]
        since: Option<String>,
    },
    /// Sessions and tokens per task label.
    Labels {
        /// Print JSON.
        #[arg(long)]
        json: bool,
    },
}

/// Load rates (refreshing when stale unless offline) and run the price step:
/// every row when the rates were just fetched, otherwise rows without a cost.
fn priced(
    store: &mut Store,
    offline: bool,
    all: bool,
) -> Result<(rates::Rates, rates::PriceStats)> {
    let path = paths::config_dir()?.join("rates.json");
    let r = rates::load(&path, offline, scan::now_ms(), rates::fetch_litellm);
    let all = all || r.origin == rates::Origin::Fetched;
    let stats = rates::price_requests(&mut store.conn, &r.file, all)?;
    Ok((r, stats))
}

fn print_price_stats(s: &rates::PriceStats) {
    println!(
        "priced: {} rows checked, {} with an estimated cost, {} without a price",
        s.rows, s.priced, s.unpriced
    );
}

/// What one read-and-enrich pass did.
struct Ingest {
    /// The per-harness lines and the enrich line, as `scan` prints them.
    lines: Vec<String>,
    files_read: u64,
    rows_added: u64,
    sessions_added: u64,
}

/// Read every harness log from its stored offset, then run the enrich step.
fn ingest(store: &mut Store, args: ScanArgs) -> Result<Ingest> {
    let mut lines = Vec::new();
    let root = match args.claude_dir {
        Some(p) => p,
        None => paths::claude_projects()?,
    };
    let r = scan::scan_claude(store, &root)?;
    let s = &r.stats;
    let (mut files_read, mut rows_added, mut sessions_added) =
        (s.files_read, s.rows_added, s.sessions_added);
    lines.push(format!(
        "claude-code: {} files seen, {} files read, {} bytes read, {} rows added, {} sessions added{}{}",
        s.files_seen,
        s.files_read,
        s.bytes_read,
        s.rows_added,
        s.sessions_added,
        if s.files_reset > 0 {
            format!(", {} files re-read after shrinking", s.files_reset)
        } else {
            String::new()
        },
        if s.bad_lines > 0 {
            format!(", {} unparseable lines skipped", s.bad_lines)
        } else {
            String::new()
        },
    ));
    let codex_home = match args.codex_dir {
        Some(p) => p,
        None => codex::default_home()?,
    };
    let c = codex::scan_codex(store, &codex_home)?;
    let cs = &c.report.stats;
    files_read += cs.files_read;
    rows_added += cs.rows_added;
    sessions_added += cs.sessions_added;
    lines.push(codex_summary_line(&c));
    let ag_dirs = if args.antigravity_dirs.is_empty() {
        antigravity::default_dirs()
    } else {
        args.antigravity_dirs
    };
    let ag = antigravity::scan_antigravity(store, &ag_dirs)?;
    let a = &ag.stats;
    files_read += a.scan.files_read;
    rows_added += a.scan.rows_added;
    sessions_added += a.scan.sessions_added;
    lines.push(format!(
        "antigravity: {} databases seen, {} databases read, {} bytes read, {} rows added, {} sessions added{}{}{}",
        a.scan.files_seen,
        a.scan.files_read,
        a.scan.bytes_read,
        a.scan.rows_added,
        a.scan.sessions_added,
        if a.scan.files_reset > 0 {
            format!(", {} databases re-read after shrinking", a.scan.files_reset)
        } else {
            String::new()
        },
        if a.immutable_opens > 0 {
            format!(", {} opened immutable (WAL ignored)", a.immutable_opens)
        } else {
            String::new()
        },
        if a.unreadable > 0 {
            format!(", {} unreadable", a.unreadable)
        } else {
            String::new()
        },
    ));
    let t3_paths = if args.t3_states.is_empty() {
        t3::default_paths()
    } else {
        args.t3_states
    };
    let e = project::enrich(&mut store.conn, &t3_paths)?;
    lines.push(format!(
        "enrich: {} sessions given a project ({} from a T3 directory), {} without a directory; t3: {} databases read{}, {} sessions linked; {:.2} s",
        e.projects_set,
        e.projects_from_t3,
        e.without_cwd,
        e.t3.databases_read,
        if e.t3.databases_unreadable > 0 {
            format!(", {} unreadable", e.t3.databases_unreadable)
        } else {
            String::new()
        },
        e.t3.sessions_linked,
        e.elapsed.as_secs_f64(),
    ));
    Ok(Ingest {
        lines,
        files_read,
        rows_added,
        sessions_added,
    })
}

/// The scan that every `query` starts with unless `--no-scan` is given:
/// incremental read, enrich, price. Prints one line to stderr only when it
/// read something, so stdout stays clean JSON.
fn catch_up(store: &mut Store, offline: bool) -> Result<()> {
    let start = std::time::Instant::now();
    let ing = ingest(store, ScanArgs::default())?;
    priced(store, offline, false)?;
    if ing.files_read > 0 {
        eprintln!(
            "scan: {} files read, {} rows added, {} sessions added ({:.2} s)",
            ing.files_read,
            ing.rows_added,
            ing.sessions_added,
            start.elapsed().as_secs_f64()
        );
    }
    Ok(())
}

/// `query session` and `query thread`. Exit 1 when nothing matches, 2 when
/// several sessions do.
fn query_session(
    store: &Store,
    offline: bool,
    key: &str,
    by_thread: bool,
    opts: (bool, bool, Option<String>),
) -> Result<()> {
    let (json, rows, since) = opts;
    let now = scan::now_ms();
    let since_ms = since_window(since, now)?;
    let lookup = if by_thread {
        query::resolve_thread(&store.conn, key)?
    } else {
        query::resolve_session(&store.conn, key)?
    };
    let what = if by_thread { "thread" } else { "session" };
    let found = match lookup {
        query::SessionLookup::Found(f) => f,
        query::SessionLookup::NotFound => {
            if json {
                let v = serde_json::json!({"error": "not found", "key": key});
                println!("{}", serde_json::to_string_pretty(&v)?);
            }
            eprintln!("{what} not found: {key}");
            std::process::exit(1);
        }
        query::SessionLookup::Ambiguous(refs) => {
            let c = query::session_candidates(&store.conn, &refs)?;
            if json {
                let v = serde_json::json!({"error": "ambiguous", "key": key, "candidates": c});
                println!("{}", serde_json::to_string_pretty(&v)?);
            } else {
                print!("{}", query::render_session_candidates(key, &c));
            }
            eprintln!("ambiguous {what} key {key:?}: pass the full session id");
            std::process::exit(2);
        }
    };
    let mut report = query::session_report(&store.conn, &found, since_ms, rows, now)?;
    let rates_path = paths::config_dir()?.join("rates.json");
    let r = rates::load(&rates_path, offline, scan::now_ms(), rates::fetch_litellm);
    report.rates_fetched_at = Some(r.file.fetched_at);
    if json {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        print!("{}", query::render_session(&report));
    }
    Ok(())
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    std::fs::create_dir_all(paths::config_dir()?)?;
    let db = match cli.db {
        Some(p) => p,
        None => paths::default_db()?,
    };
    if let Command::Serve { port, no_open } = cli.command {
        // Read-only: the dashboard never opens the store for writing.
        return serve::run(
            &db,
            serve::Options {
                port,
                open_browser: !no_open,
            },
        );
    }
    let mut store = Store::open(&db)?;
    if !cli.no_scan && matches!(cli.command, Command::Query { .. }) {
        catch_up(&mut store, cli.offline)?;
    }
    match cli.command {
        Command::Mcp => {
            let (offline, no_scan) = (cli.offline, cli.no_scan);
            mcp::serve(
                store,
                offline,
                no_scan,
                Box::new(move |s| catch_up(s, offline)),
            )?;
        }
        Command::Scan { args } => {
            let scan_start = std::time::Instant::now();
            let ing = ingest(&mut store, args)?;
            for l in &ing.lines {
                println!("{l}");
            }
            println!("elapsed: {:.2} s", scan_start.elapsed().as_secs_f64());
            let (_, stats) = priced(&mut store, cli.offline, false)?;
            print_price_stats(&stats);
        }
        Command::Rebuild { args } => {
            let start = std::time::Instant::now();
            let tx = store.conn.transaction()?;
            // Children first; `session_labels` is not touched.
            for table in ["codex_cursors", "requests", "sessions", "sources"] {
                tx.execute(&format!("DELETE FROM {table}"), [])?;
            }
            tx.commit()?;
            println!(
                "rebuild: cleared sources, sessions, requests and codex cursors; kept session_labels"
            );
            let ing = ingest(&mut store, args)?;
            for l in &ing.lines {
                println!("{l}");
            }
            let (_, stats) = priced(&mut store, cli.offline, true)?;
            print_price_stats(&stats);
            println!("rebuild elapsed: {:.2} s", start.elapsed().as_secs_f64());
        }
        Command::Query {
            what:
                QueryCommand::Summary {
                    group_by,
                    json,
                    since,
                },
        } => {
            let now = scan::now_ms();
            let since_ms = since
                .map(|s| query::parse_since(&s))
                .transpose()?
                .map(|w| now - w);
            let (r, _) = priced(&mut store, cli.offline, false)?;
            match group_by {
                GroupBy::ToolModel => {
                    let mut s = query::summary(&store.conn, since_ms, now)?;
                    s.rates_fetched_at = Some(r.file.fetched_at);
                    if json {
                        println!("{}", serde_json::to_string_pretty(&s)?);
                    } else {
                        print!("{}", query::render_summary_table(&s));
                    }
                }
                GroupBy::Label => {
                    let mut s = labels::summary_by_label(&store.conn, since_ms, now)?;
                    s.rates_fetched_at = Some(r.file.fetched_at);
                    if json {
                        println!("{}", serde_json::to_string_pretty(&s)?);
                    } else {
                        print!("{}", labels::render_label_summary_table(&s));
                    }
                }
            }
        }
        Command::Query {
            what:
                QueryCommand::Session {
                    key,
                    json,
                    rows,
                    since,
                },
        } => query_session(&store, cli.offline, &key, false, (json, rows, since))?,
        Command::Query {
            what:
                QueryCommand::Thread {
                    thread_id,
                    json,
                    rows,
                    since,
                },
        } => query_session(&store, cli.offline, &thread_id, true, (json, rows, since))?,
        Command::Query {
            what: QueryCommand::Agents { json, since },
        } => {
            let now = scan::now_ms();
            let since_ms = since
                .map(|s| query::parse_since(&s))
                .transpose()?
                .map(|w| now - w);
            let report = agents::agents_report(&store.conn, since_ms, now)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&report)?);
            } else {
                print!("{}", agents::render_agents(&report));
            }
        }
        Command::Query {
            what: QueryCommand::Labels { json },
        } => {
            let (_, _) = priced(&mut store, cli.offline, false)?;
            let report = labels::label_report(&store.conn, scan::now_ms())?;
            if json {
                println!("{}", serde_json::to_string_pretty(&report)?);
            } else {
                print!("{}", labels::render_label_table(&report));
            }
        }
        Command::Label { session, kind } => {
            let (tool, id) = labels::resolve_session(&store.conn, &session)?;
            let before = labels::set_manual_label(&store.conn, &tool, &id, &kind, scan::now_ms())?;
            println!(
                "{tool} {id}: label set to {}{}",
                labels::parse_kind(&kind).unwrap_or_default(),
                match before {
                    Some(b) => format!(" (was {b})"),
                    None => String::new(),
                }
            );
        }
        Command::Query {
            what: QueryCommand::Models { json },
        } => {
            let (r, _) = priced(&mut store, cli.offline, false)?;
            let rows = rates::model_rows(&store.conn, &r.file)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&rows)?);
            } else {
                print!("{}", rates::render_models_table(&rows));
            }
        }
        Command::Query {
            what: QueryCommand::Project { key, json, since },
        } => {
            let now = scan::now_ms();
            let since_ms = since_window(since, now)?;
            let found = match project::lookup(&store.conn, &key)? {
                project::Lookup::Found(p) => p,
                project::Lookup::NotFound => {
                    if json {
                        let v = serde_json::json!({"error": "not found", "key": key});
                        println!("{}", serde_json::to_string_pretty(&v)?);
                    }
                    eprintln!("project not found: {key}");
                    std::process::exit(1);
                }
                project::Lookup::Ambiguous(c) => {
                    if json {
                        let v =
                            serde_json::json!({"error": "ambiguous", "key": key, "candidates": c});
                        println!("{}", serde_json::to_string_pretty(&v)?);
                    } else {
                        print!("{}", project::render_candidates_table(&key, &c));
                    }
                    eprintln!("ambiguous project name {key:?}: pass the full path");
                    std::process::exit(2);
                }
            };
            let (r, _) = priced(&mut store, cli.offline, false)?;
            let mut d = project::detail(&store.conn, &found, since_ms, now)?;
            d.rates_fetched_at = Some(r.file.fetched_at);
            if json {
                println!("{}", serde_json::to_string_pretty(&d)?);
            } else {
                print!("{}", project::render_detail_table(&d));
            }
        }
        Command::Query {
            what: QueryCommand::Projects { json, since },
        } => {
            let now = scan::now_ms();
            let since_ms = since_window(since, now)?;
            let (r, _) = priced(&mut store, cli.offline, false)?;
            let mut l = project::list(&store.conn, since_ms, now)?;
            l.rates_fetched_at = Some(r.file.fetched_at);
            if json {
                println!("{}", serde_json::to_string_pretty(&l)?);
            } else {
                print!("{}", project::render_list_table(&l));
            }
        }
        Command::Rates {
            what: RatesCommand::Update,
        } => {
            if cli.offline {
                anyhow::bail!("rates update fetches from the network; drop --offline");
            }
            let path = paths::config_dir()?.join("rates.json");
            let file = rates::update(&path, scan::now_ms(), rates::fetch_litellm)?;
            println!(
                "wrote {}: {} models, fetched {}",
                path.display(),
                file.models.len(),
                file.fetched_at
            );
            print_price_stats(&rates::price_requests(&mut store.conn, &file, true)?);
        }
        Command::Rates {
            what: RatesCommand::Apply { all },
        } => {
            let (r, stats) = priced(&mut store, cli.offline, all)?;
            println!(
                "rates: {} models, fetched {} ({})",
                r.file.models.len(),
                r.file.fetched_at,
                match r.origin {
                    rates::Origin::Fetched => "just fetched",
                    rates::Origin::File => "rates.json",
                    rates::Origin::Snapshot => "built-in snapshot",
                }
            );
            print_price_stats(&stats);
        }
        Command::Serve { .. } => unreachable!("handled before the store is opened"),
    }
    Ok(())
}

/// `--since 7d` as an absolute lower bound in epoch ms.
fn since_window(since: Option<String>, now_ms: i64) -> Result<Option<i64>> {
    Ok(since
        .map(|s| query::parse_since(&s))
        .transpose()?
        .map(|w| now_ms - w))
}

fn codex_summary_line(c: &codex::CodexScanReport) -> String {
    let s = &c.report.stats;
    let a = &c.anomalies;
    let mut line = format!(
        "codex: {} files seen, {} files read, {} bytes read, {} rows added, {} sessions added",
        s.files_seen, s.files_read, s.bytes_read, s.rows_added, s.sessions_added
    );
    for (n, what) in [
        (s.files_reset, "files re-read after shrinking"),
        (
            c.files_relocated,
            "files moved or compressed since last scan",
        ),
        (c.duplicates_skipped, "duplicate rollout copies skipped"),
        (c.files_unreadable, "files unreadable this time"),
        (s.bad_lines, "unparseable lines skipped"),
        (
            a.cached_exceeds_input,
            "requests with cached > input (clamped)",
        ),
        (
            a.usage_mismatch,
            "requests where last usage != cumulative delta",
        ),
        (a.counter_resets, "cumulative counter restarts"),
        (
            a.repeated_token_counts,
            "repeated token_count events skipped",
        ),
        (
            a.copied_history_skipped,
            "usage events in copied fork history skipped",
        ),
        (
            a.token_count_without_info,
            "token_count events without usage",
        ),
        (
            a.foreign_usage_records,
            "usage records for another thread skipped",
        ),
    ] {
        if n > 0 {
            line.push_str(&format!(", {n} {what}"));
        }
    }
    line
}
