//! Terminal rendering: the summary, the per-provider detail and the compact form.

use crate::report::{Report, Window, status, until};
use chrono::{DateTime, Utc};

pub struct Painter {
    pub color: bool,
}

impl Painter {
    fn paint(&self, code: &str, s: &str) -> String {
        if self.color { format!("\x1b[{code}m{s}\x1b[0m") } else { s.to_string() }
    }

    fn level(&self, used: f64, s: &str) -> String {
        let code = match used {
            p if p >= 90.0 => "31",
            p if p >= 70.0 => "33",
            _ => "32",
        };
        self.paint(code, s)
    }
}

/// What can be used now, then the compact line for every provider.
pub fn print_summary(reports: &[Report], p: &Painter) {
    let usable: Vec<String> = reports.iter().filter_map(|r| status(r).usable).collect();
    let none = p.paint("2", "nothing");
    let usable = if usable.is_empty() { none } else { usable.join(", ") };
    println!("{} {}", p.paint("1", "Usable now:"), p.paint("32", &usable));
    print_compact(reports, p);
}

pub fn print_report(r: &Report, p: &Painter) {
    let plan = r.plan.as_deref().map(|plan| format!(" ({plan})")).unwrap_or_default();
    println!("{}{}", p.paint("1", r.provider), p.paint("2", &plan));
    if let Some(err) = &r.error {
        println!("  {}", p.paint("33", &format!("unavailable: {err}")));
    }
    let width = r.windows.iter().map(|w| w.label.chars().count()).max().unwrap_or(0);
    for w in &r.windows {
        let filled = ((w.used_percent / 5.0).round() as usize).min(20);
        let bar = format!("{}{}", "█".repeat(filled), "░".repeat(20 - filled));
        let mut line = format!(
            "  {:<width$}  {}  {:>4}",
            w.label,
            p.level(w.used_percent, &bar),
            format!("{:.0}%", w.used_percent),
        );
        if let Some(d) = &w.detail {
            line.push_str(&format!("  {d}"));
        }
        if w.blocked {
            line.push_str(&format!("  {}", p.paint("31", "blocked")));
        }
        if let Some(t) = w.resets_at {
            line.push_str(&p.paint("2", &format!("  resets in {}", until(t))));
        }
        if let Some(t) = w.runs_out_at {
            line.push_str(&format!("  {}", p.paint("33", &format!("runs out in ~{} at this pace", until(t)))));
        }
        println!("{line}");
    }
    for note in &r.notes {
        println!("  {}", p.paint("2", note));
    }
    for advice in &r.advice {
        println!("  {}", p.paint("33", &format!("→ {advice}")));
    }
}

/// One line per provider with every window's usage and time to reset, as
/// `98% - 1h11m`. Provider-wide windows come first, then each pool with several
/// windows as `Pool: a% - 3h2m, b% - 4d1h0m`, then single-window model caps as
/// `Model: c% - 2d3h4m`. `Provider: ?` when it could not be read.
pub fn print_compact(reports: &[Report], p: &Painter) {
    for r in reports {
        if r.error.is_some() {
            println!("{}", p.paint("33", &format!("{}: ?", r.provider)));
            continue;
        }
        let cell = |w: &Window| {
            let pct = p.level(w.used_percent, &format!("{:.0}%", w.used_percent));
            match w.resets_at {
                Some(t) => format!("{pct} - {}", countdown(t)),
                None => pct,
            }
        };
        let mut parts: Vec<String> = r.windows.iter().filter(|w| w.pool.is_none()).map(cell).collect();
        let mut pools: Vec<&str> = Vec::new();
        for pool in r.windows.iter().filter_map(|w| w.pool.as_deref()) {
            if !pools.contains(&pool) {
                pools.push(pool);
            }
        }
        let mut singles = Vec::new();
        for pool in pools {
            let cells: Vec<String> =
                r.windows.iter().filter(|w| w.pool.as_deref() == Some(pool)).map(cell).collect();
            let line = format!("{pool}: {}", cells.join(", "));
            if cells.len() == 1 { singles.push(line) } else { parts.push(line) }
        }
        parts.extend(singles);
        println!("{}: {}", p.paint("1", r.provider), parts.join(", "));
    }
}

/// Time until `t` with no spaces and every unit down to minutes: "11m", "1h11m", "5d1h3m".
fn countdown(t: DateTime<Utc>) -> String {
    let mins = ((t - Utc::now()).num_seconds().max(0) + 59) / 60;
    let (d, h, m) = (mins / 1440, mins % 1440 / 60, mins % 60);
    match (d, h) {
        (0, 0) => format!("{m}m"),
        (0, _) => format!("{h}h{m}m"),
        _ => format!("{d}d{h}h{m}m"),
    }
}
