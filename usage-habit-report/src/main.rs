use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use usage_habit_report::{paths, query, rates, scan, store::Store};

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
        Command::Scan { claude_dir } => {
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
            println!("elapsed: {:.2} s", r.elapsed.as_secs_f64());
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
