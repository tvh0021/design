//! Terminal rendering: the summary, the per-provider detail and the compact form.

use crate::hosts::tag;
use crate::report::{HOST_PROVIDER, Report, Window, status, until};
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

/// The provider's name, tagged with its hosts when not every reachable host
/// has this account (`Antigravity @rpi5`); for a host error, the host's name.
fn name(r: &Report, reachable: &[String]) -> String {
    if r.provider == HOST_PROVIDER {
        let names: Vec<&str> = r.hosts.iter().map(|h| h.name.as_str()).collect();
        return names.join(",");
    }
    format!("{}{}", r.provider, tag(r, reachable))
}

/// What can be used now, then the compact line for every provider.
/// `reachable` names every host that was read.
pub fn print_summary(reports: &[Report], reachable: &[String], p: &Painter) {
    let usable: Vec<String> = reports.iter().filter_map(|r| status(r, &name(r, reachable)).usable).collect();
    let none = p.paint("2", "nothing");
    let usable = if usable.is_empty() { none } else { usable.join(", ") };
    println!("{} {}", p.paint("1", "Usable now:"), p.paint("32", &usable));
    print_compact(reports, reachable, p);
}

pub fn print_report(r: &Report, reachable: &[String], p: &Painter) {
    if r.provider == HOST_PROVIDER {
        println!("{}", p.paint("1", &name(r, reachable)));
        println!("  {}", p.paint("33", r.error.as_deref().unwrap_or("unavailable")));
        return;
    }
    let plan = r.plan.as_deref().map(|plan| format!(" ({plan})")).unwrap_or_default();
    println!("{}{}", p.paint("1", &name(r, reachable)), p.paint("2", &plan));
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
/// `Model: c% - 2d3h4m`. `Provider: ?` when it could not be read, and
/// `host: why` for a host that could not be read.
pub fn print_compact(reports: &[Report], reachable: &[String], p: &Painter) {
    for line in compact_lines(reports, reachable, p) {
        println!("{line}");
    }
}

fn compact_lines(reports: &[Report], reachable: &[String], p: &Painter) -> Vec<String> {
    let mut lines = Vec::new();
    for r in reports {
        let name = name(r, reachable);
        if r.provider == HOST_PROVIDER {
            lines.push(p.paint("33", &format!("{name}: {}", r.error.as_deref().unwrap_or("unavailable"))));
            continue;
        }
        if r.error.is_some() {
            lines.push(p.paint("33", &format!("{name}: ?")));
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
        lines.push(format!("{}: {}", p.paint("1", &name), parts.join(", ")));
    }
    lines
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hosts::merge;
    use crate::report::HostRef;

    fn on(hosts: &[&str], provider: &str, used: f64) -> Report {
        Report {
            provider: provider.into(),
            windows: vec![Window::new("Weekly", used, None, None)],
            hosts: hosts.iter().map(|h| HostRef { name: h.to_string(), t3_environment: None }).collect(),
            ..Default::default()
        }
    }

    #[test]
    fn compact_tags_accounts_not_on_every_host() {
        let reports = vec![
            on(&["mac", "rpi5"], "Claude", 10.0),
            on(&["mac"], "Antigravity", 20.0),
            on(&["rpi5"], "Antigravity", 30.0),
        ];
        let p = Painter { color: false };
        let both = ["mac".to_string(), "rpi5".to_string()];
        assert_eq!(
            compact_lines(&reports, &both, &p),
            ["Claude: 10%", "Antigravity @mac: 20%", "Antigravity @rpi5: 30%"]
        );
        // One host read: no tags, as before.
        assert_eq!(compact_lines(&reports[..2], &both[..1], &p), ["Claude: 10%", "Antigravity: 20%"]);
    }

    #[test]
    fn compact_shows_host_errors_and_json_stays_an_array() {
        let mac = vec![on(&["mac"], "Claude", 10.0)];
        let rpi5 = HostRef { name: "rpi5".into(), t3_environment: Some("rpi5".into()) };
        let reports = merge(mac, vec![(rpi5, Err("unreachable (timeout)".into()))]);
        let p = Painter { color: false };
        assert_eq!(
            compact_lines(&reports, &["mac".to_string()], &p),
            ["Claude: 10%", "rpi5: unreachable (timeout)"]
        );
        let json = serde_json::to_value(&reports).unwrap();
        let elements = json.as_array().expect("top level is an array");
        assert_eq!(
            elements[1],
            serde_json::json!({
                "provider": "Host",
                "windows": [],
                "error": "unreachable (timeout)",
                "account": null,
                "signed_in": true,
                "stale": false,
                "hosts": [{"name": "rpi5", "t3_environment": "rpi5"}]
            })
        );
    }
}
