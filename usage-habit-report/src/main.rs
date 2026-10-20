use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use usage_habit_report::{
    antigravity, codex, paths, project, query, rates, scan, store::Store, t3,
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
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Read new bytes from every harness log into the store.
    Scan {
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
    },
    /// Read from the store.
    Query {
        #[command(subcommand)]
        what: QueryCommand,
    },
    /// Model prices (LiteLLM) and request costs.
    Rates {
        #[command(subcommand)]
        what: RatesCommand,
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

#[derive(Subcommand)]
enum QueryCommand {
    /// Token totals grouped by tool and model.
    Summary {
        /// Print JSON (shape documented in README.md).
        #[arg(long)]
        json: bool,
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
    /// Every project with totals.
    Projects {
        /// Print JSON (shape documented in README.md).
        #[arg(long)]
        json: bool,
        /// Only requests in the last N hours/days/weeks: 12h, 7d, 2w.
        #[arg(long)]
        since: Option<String>,
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

fn main() -> Result<()> {
    let cli = Cli::parse();
    std::fs::create_dir_all(paths::config_dir()?)?;
    let db = match cli.db {
        Some(p) => p,
        None => paths::default_db()?,
    };
    let mut store = Store::open(&db)?;
    match cli.command {
        Command::Scan {
            claude_dir,
            codex_dir,
            antigravity_dirs,
            t3_states,
        } => {
            let scan_start = std::time::Instant::now();
            let root = match claude_dir {
                Some(p) => p,
                None => paths::claude_projects()?,
            };
            let r = scan::scan_claude(&mut store, &root)?;
            let s = &r.stats;
            println!(
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
            );
            let codex_home = match codex_dir {
                Some(p) => p,
                None => codex::default_home()?,
            };
            let c = codex::scan_codex(&mut store, &codex_home)?;
            println!("{}", codex_summary_line(&c));
            let ag_dirs = if antigravity_dirs.is_empty() {
                antigravity::default_dirs()
            } else {
                antigravity_dirs
            };
            let ag = antigravity::scan_antigravity(&mut store, &ag_dirs)?;
            let a = &ag.stats;
            println!(
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
            );
            let t3_paths = if t3_states.is_empty() {
                t3::default_paths()
            } else {
                t3_states
            };
            let e = project::enrich(&mut store.conn, &t3_paths)?;
            println!(
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
            );
            println!("elapsed: {:.2} s", scan_start.elapsed().as_secs_f64());
            let (_, stats) = priced(&mut store, cli.offline, false)?;
            print_price_stats(&stats);
        }
        Command::Query {
            what: QueryCommand::Summary { json, since },
        } => {
            let now = scan::now_ms();
            let since_ms = since
                .map(|s| query::parse_since(&s))
                .transpose()?
                .map(|w| now - w);
            let (r, _) = priced(&mut store, cli.offline, false)?;
            let mut s = query::summary(&store.conn, since_ms, now)?;
            s.rates_fetched_at = Some(r.file.fetched_at);
            if json {
                println!("{}", serde_json::to_string_pretty(&s)?);
            } else {
                print!("{}", query::render_summary_table(&s));
            }
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
