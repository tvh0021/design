//! Show current usage limits for every AI subscription on this machine and,
//! with a hosts config, on other machines reached over ssh.
//!
//! Reads the same sources the T3 Code usage dashboard does
//! (`apps/server/src/provider/*UsageLimits.ts` in t3code-dev), using the
//! credentials each tool already stores locally:
//!
//! - Claude: Claude Code's OAuth token in the macOS keychain or `~/.claude/.credentials.json`
//! - Codex: `~/.codex/auth.json` (ChatGPT login)
//! - Antigravity: T3's `acp_token.json` (Google OAuth refresh token)
//! - ChatLLM (Abacus): T3's stored `ABACUS_API_KEY`, or the env var
//! - Zed: T3's stored dashboard session cookie, or `ZED_DASHBOARD_SESSION`
//!
//! Claude and Codex tokens are never refreshed here: their refresh tokens
//! rotate, so refreshing would sign the real CLI out. Run the CLI once if
//! its token has expired. Antigravity's short-lived access token is cached
//! in the user cache directory (`~/Library/Caches/ai-usage` on macOS,
//! `$XDG_CACHE_HOME/ai-usage` or `~/.cache/ai-usage` on Linux).
//!
//! Other machines are listed in `$XDG_CONFIG_HOME/ai-usage/hosts.json`
//! (default `~/.config/ai-usage/hosts.json`, on macOS too). Each one runs
//! `ai-usage --local --json` over ssh, in parallel with the local probes.
//! Every report carries a fingerprint of its account, so an account signed in
//! on several machines shows once, listing them all, and different accounts
//! of one provider show separately, tagged with their machine.

mod hosts;
mod output;
mod providers;
mod report;

use std::io::{IsTerminal, Write};
use std::process::ExitCode;
use std::time::Duration;

use chrono::{Local, Utc};

use hosts::Config;
use output::Painter;
use providers::PROVIDERS;
use report::{HostRef, Report};

const USAGE: &str = "\
Usage: ai-usage [options] [provider ...]

Providers: claude, codex, antigravity, chatllm, zed (default: all)

Options:
  --compact          one line per provider with every window's usage
  --json             machine-readable output: an array of reports, each with
                     its account fingerprint and the hosts signed in to it
  --local            read this machine only, not the hosts in the config
  --watch [SECS]     refresh every SECS seconds (default 60) until Ctrl-C;
                     terminal only
  --fail-above PCT   exit with status 1 if any window is more than PCT% used
  -h, --help         show this help

Hosts: ~/.config/ai-usage/hosts.json (or $XDG_CONFIG_HOME/ai-usage/hosts.json)
lists other machines to read over ssh, e.g.
  {\"self\": {\"name\": \"mac\", \"t3_environment\": \"MacBook Pro\"},
   \"hosts\": [{\"name\": \"rpi5\", \"ssh\": \"rpi5\", \"t3_environment\": \"rpi5\"}]}
Each host runs `ai-usage --local --json` there. An account signed in on
several hosts shows once; one that is not on every host is tagged with its
hosts, as `Antigravity @rpi5`.";

struct Options {
    json: bool,
    compact: bool,
    local: bool,
    watch: Option<u64>,
    fail_above: Option<f64>,
    providers: Vec<String>,
}

fn parse_args() -> Result<Options, String> {
    let mut opts = Options { json: false, compact: false, local: false, watch: None, fail_above: None, providers: Vec::new() };
    let mut args = std::env::args().skip(1).peekable();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                println!("{USAGE}");
                std::process::exit(0);
            }
            "--json" => opts.json = true,
            "--compact" => opts.compact = true,
            "--local" => opts.local = true,
            "--watch" => {
                let secs = args.next_if(|a| a.parse::<u64>().is_ok()).map(|a| a.parse().unwrap());
                opts.watch = Some(secs.unwrap_or(60).max(10));
            }
            "--fail-above" => {
                let pct = args.next().and_then(|a| a.parse::<f64>().ok());
                opts.fail_above = Some(pct.ok_or("--fail-above needs a percentage")?);
            }
            flag if flag.starts_with('-') => return Err(format!("unknown option {flag}")),
            name => {
                let name = name.to_lowercase();
                if !PROVIDERS.iter().any(|(p, _)| p.to_lowercase() == name) {
                    return Err(format!("unknown provider {name}"));
                }
                opts.providers.push(name);
            }
        }
    }
    if opts.json && opts.compact {
        return Err("--json and --compact cannot be combined".into());
    }
    if opts.watch.is_some() && opts.fail_above.is_some() {
        return Err("--watch and --fail-above cannot be combined".into());
    }
    Ok(opts)
}

/// Runs the selected providers on this machine in parallel, keeping their order.
fn collect_local(wanted: &[String], me: &HostRef) -> Vec<Report> {
    let selected: Vec<_> = PROVIDERS
        .iter()
        .filter(|(name, _)| wanted.is_empty() || wanted.contains(&name.to_lowercase()))
        .collect();
    let now = Utc::now();
    std::thread::scope(|s| {
        let handles: Vec<_> = selected.iter().map(|&&(name, probe)| (name, s.spawn(probe))).collect();
        handles
            .into_iter()
            .map(|(name, h)| {
                let result = h.join().unwrap_or_else(|_| Err("probe panicked".into()));
                let mut report = result
                    .unwrap_or_else(|f| Report {
                        provider: name.into(),
                        error: Some(f.message),
                        signed_in: f.signed_in,
                        ..Default::default()
                    })
                    .finish(now);
                report.hosts = vec![me.clone()];
                report
            })
            .collect()
    })
}

/// Reads this machine and every configured host at once, then merges them.
/// Returns the reports and the names of the hosts that could be read.
fn collect(wanted: &[String], config: &Config) -> (Vec<Report>, Vec<String>) {
    let (local, remotes) = std::thread::scope(|s| {
        let remote: Vec<_> = config.hosts.iter().map(|h| (h.host_ref(), s.spawn(|| hosts::fetch(h, wanted)))).collect();
        let local = collect_local(wanted, &config.me);
        let remotes: Vec<_> = remote
            .into_iter()
            .map(|(host, h)| (host, h.join().unwrap_or_else(|_| Err("fetch panicked".into()))))
            .collect();
        (local, remotes)
    });
    let mut reachable = vec![config.me.name.clone()];
    reachable.extend(remotes.iter().filter(|(_, r)| r.is_ok()).map(|(h, _)| h.name.clone()));
    (hosts::merge(local, remotes), reachable)
}

fn render(reports: &[Report], reachable: &[String], opts: &Options, painter: &Painter) {
    if opts.json {
        println!("{}", serde_json::to_string_pretty(reports).unwrap());
    } else if opts.compact {
        output::print_compact(reports, reachable, painter);
    } else {
        output::print_summary(reports, reachable, painter);
        for r in reports {
            println!();
            output::print_report(r, reachable, painter);
        }
    }
}

fn main() -> ExitCode {
    let opts = match parse_args() {
        Ok(o) => o,
        Err(e) => {
            eprintln!("ai-usage: {e}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let tty = std::io::stdout().is_terminal();
    let painter = Painter { color: tty && std::env::var_os("NO_COLOR").is_none() };
    // Watch mode never exits. Without a terminal there is no one to press
    // Ctrl-C (an agent's shell, a pipe), so it would only hang.
    if opts.watch.is_some() && !tty {
        eprintln!("ai-usage: --watch needs a terminal; run ai-usage once per check instead");
        return ExitCode::from(2);
    }
    // `--local` never reads the hosts config, so a broken one cannot stop it.
    let config = if opts.local {
        hosts::local_config()
    } else {
        match hosts::load_config() {
            Ok(c) => c,
            Err(e) => {
                eprintln!("ai-usage: bad hosts config {e}");
                return ExitCode::from(2);
            }
        }
    };

    if let Some(secs) = opts.watch {
        loop {
            let (reports, reachable) = collect(&opts.providers, &config);
            if !opts.json {
                print!("\x1b[2J\x1b[H");
            }
            if !opts.json && !opts.compact {
                let header = format!("Updated {} · every {secs}s · Ctrl-C to stop\n", Local::now().format("%H:%M:%S"));
                println!("{}", if painter.color { format!("\x1b[2m{header}\x1b[0m") } else { header });
            }
            render(&reports, &reachable, &opts, &painter);
            let _ = std::io::stdout().flush();
            std::thread::sleep(Duration::from_secs(secs));
        }
    }

    let (reports, reachable) = collect(&opts.providers, &config);
    render(&reports, &reachable, &opts, &painter);
    match opts.fail_above {
        Some(limit) if reports.iter().flat_map(|r| &r.windows).any(|w| w.used_percent > limit) => ExitCode::from(1),
        _ => ExitCode::SUCCESS,
    }
}
