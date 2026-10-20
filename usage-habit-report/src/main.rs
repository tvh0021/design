use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use usage_habit_report::{
    agents, antigravity, codex, labels, paths, query, rates, scan, store::Store,
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
    },
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
    /// Raw model names with their canonical name and whether they have a price.
    Models {
        /// Print JSON.
        #[arg(long)]
        json: bool,
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
            println!("elapsed: {:.2} s", scan_start.elapsed().as_secs_f64());
            let (_, stats) = priced(&mut store, cli.offline, false)?;
            print_price_stats(&stats);
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
