use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use usage_habit_report::{antigravity, codex, paths, query, scan, store::Store};

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
        }
        Command::Query {
            what: QueryCommand::Summary { json, since },
        } => {
            let now = scan::now_ms();
            let since_ms = since
                .map(|s| query::parse_since(&s))
                .transpose()?
                .map(|w| now - w);
            let s = query::summary(&store.conn, since_ms, now)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&s)?);
            } else {
                print!("{}", query::render_summary_table(&s));
            }
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
