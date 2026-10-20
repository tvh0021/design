//! Terminal rendering: the summary, the per-provider detail and the one-line compact form.

use crate::report::{Report, Window, status, until};

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

/// What can be used now, what is blocked and until when, what could not be read.
pub fn print_summary(reports: &[Report], p: &Painter) {
    let mut usable = Vec::new();
    let mut blocked: Vec<(String, &Window)> = Vec::new();
    for r in reports {
        let s = status(r);
        usable.extend(s.usable);
        blocked.extend(s.blocked.into_iter().map(|w| (format!("{} {}", r.provider, w.label), w)));
    }
    let unavailable: Vec<&Report> = reports.iter().filter(|r| r.error.is_some()).collect();

    let none = p.paint("2", "nothing");
    let usable = if usable.is_empty() { none } else { usable.join(", ") };
    println!("{} {}", p.paint("1", "Usable now:"), p.paint("32", &usable));
    if !blocked.is_empty() {
        println!("{}", p.paint("1", "Blocked:"));
        let width = blocked.iter().map(|(name, _)| name.chars().count()).max().unwrap_or(0);
        for (name, w) in &blocked {
            let reset = w.resets_at.map(|t| format!("resets in {}", until(t))).unwrap_or_default();
            println!("  {}  {}", p.paint("31", &format!("{name:<width$}")), p.paint("2", &reset));
        }
    }
    if !unavailable.is_empty() {
        let names: Vec<&str> = unavailable.iter().map(|r| r.provider).collect();
        println!("{} {}", p.paint("1", "Unavailable:"), p.paint("33", &names.join(", ")));
    }
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

/// One line for a status bar: each provider's highest usable window, or when it unblocks.
pub fn print_compact(reports: &[Report], p: &Painter) {
    let parts: Vec<String> = reports
        .iter()
        .map(|r| {
            let s = status(r);
            match (r.error.is_some(), s.worst_usable) {
                (true, _) => p.paint("33", &format!("{} ?", r.provider)),
                (false, Some(used)) => format!("{} {}", r.provider, p.level(used, &format!("{used:.0}%"))),
                (false, None) => {
                    let when = s.unblocks_at.map(|t| format!(" {}", until(t))).unwrap_or_default();
                    p.paint("31", &format!("{} blocked{when}", r.provider))
                }
            }
        })
        .collect();
    println!("{}", parts.join(" · "));
}
