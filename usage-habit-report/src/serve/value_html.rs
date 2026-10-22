//! The secondary page: what each subscription buys. Same frame, tokens and
//! helpers as the main dashboard (`html`), with the figures of `value`.
//! One section per tool after a summary ledger; see docs/design.md,
//! "Subscription value page".

use super::data::StoreFacts;
use super::html::{Audience, CSS, esc, fmt_int, section};
use crate::value::{Capacity, Run, ToolValue, ValueReport, WindowEstimate};
use std::fmt::Write;

/// Everything the page shows, already computed; no database here.
#[derive(Debug, Clone)]
pub struct ValuePage {
    pub audience: Audience,
    /// "Tue 20 Oct 2026, 00:56"
    pub now_label: String,
    /// "UTC-04:00"
    pub tz_label: String,
    pub facts: StoreFacts,
    pub report: ValueReport,
    /// Local clock labels of each run's start and end, in `report` order:
    /// `[tool][window][run] = (start, end)`.
    pub run_labels: Vec<Vec<Vec<(String, String)>>>,
    pub rates_fetched_at: Option<String>,
    pub load_ms: f64,
}

impl ValuePage {
    /// Round every dollar figure for the public export, as the main page does.
    pub fn round_costs(&mut self) {
        fn cap(c: &mut Capacity) {
            c.estimated_cost_usd = c.estimated_cost_usd.map(f64::round);
        }
        for t in &mut self.report.tools {
            t.mean_monthly_cost_usd = t.mean_monthly_cost_usd.map(f64::round);
            for w in &mut t.windows {
                for r in &mut w.runs {
                    r.usage.estimated_cost_usd = r.usage.estimated_cost_usd.map(f64::round);
                    cap(&mut r.per_window);
                }
                if let Some(c) = &mut w.per_window {
                    cap(c);
                }
                if let Some(c) = &mut w.lower_bound {
                    cap(c);
                }
                for d in &mut w.drift {
                    cap(&mut d.per_window);
                }
            }
            for m in &mut t.months {
                m.usage.estimated_cost_usd = m.usage.estimated_cost_usd.map(f64::round);
            }
        }
    }

    fn usd(&self, cost: Option<f64>) -> String {
        if self.audience.is_public() {
            cost.map_or("-".into(), |c| format!("${c:.0}"))
        } else {
            cost.map_or("-".into(), |c| format!("${c:.2}"))
        }
    }
}

/// "12.3M" for millions, plain thousands below that.
fn fmt_tokens(n: f64) -> String {
    if n >= 1e6 {
        format!("{:.1}M", n / 1e6)
    } else {
        fmt_int(n.round() as i64)
    }
}

fn fmt_x(x: Option<f64>) -> String {
    x.map_or("-".into(), |x| format!("{x:.1}x"))
}

fn price(t: &ToolValue, p: &ValuePage) -> String {
    match t.subscription.monthly_usd {
        Some(c) => format!("{} a month", p.usd(Some(c))),
        None => "no price set".into(),
    }
}

fn window_name(w: &WindowEstimate) -> String {
    if w.window == "weekly" {
        "weekly (168 h)".into()
    } else {
        format!("session ({} h)", w.window_hours)
    }
}

fn self_href(p: &ValuePage) -> &'static str {
    if p.audience.is_public() {
        super::snapshot::VALUE_FILENAME
    } else {
        "/value"
    }
}

fn index_href(p: &ValuePage) -> &'static str {
    if p.audience.is_public() {
        "index.html"
    } else {
        "/"
    }
}

fn summary(p: &ValuePage) -> String {
    let mut s = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\" class=\"l\">Plan</th><th scope=\"col\">Price a month</th><th scope=\"col\">Est. API cost a month</th><th scope=\"col\">Times the price</th><th scope=\"col\">Est. cost, full 5 h</th><th scope=\"col\">Sessions, full 5 h</th><th scope=\"col\">Est. cost, full week</th><th scope=\"col\">Limit readings</th></tr></thead><tbody>",
    );
    for t in &p.report.tools {
        let cell = |w: Option<&WindowEstimate>, f: &dyn Fn(&Capacity) -> String| match w {
            Some(w) => match (&w.per_window, &w.lower_bound) {
                (Some(c), _) => f(c),
                (None, Some(b)) => format!("at least {}", f(b)),
                (None, None) => "-".into(),
            },
            None => "-".into(),
        };
        let session = t.windows.iter().find(|w| w.window == "session");
        let weekly = t.windows.iter().find(|w| w.window == "weekly");
        let _ = write!(
            s,
            "<tr><th scope=\"row\"><span class=\"tool\" data-tool=\"{0}\">{0}</span></th><td class=\"l\">{1}</td><td>{2}</td><td>{3}</td><td>{4}</td><td>{5}</td><td>{6}</td><td>{7}</td><td>{8}</td></tr>",
            esc(&t.tool),
            esc(&t.subscription.name),
            p.usd(t.subscription.monthly_usd),
            p.usd(t.mean_monthly_cost_usd),
            fmt_x(t.mean_multiple),
            cell(session, &|c| p.usd(c.estimated_cost_usd)),
            cell(session, &|c| format!("{:.1}", c.sessions)),
            cell(weekly, &|c| p.usd(c.estimated_cost_usd)),
            match t.limit_source.as_str() {
                "readings" => fmt_int(t.windows.iter().map(|w| w.readings).sum()),
                "rejections" => format!(
                    "{} rejections",
                    fmt_int(t.windows.iter().map(|w| w.readings).sum())
                ),
                _ => "none".into(),
            }
        );
    }
    s.push_str("</tbody></table></div>");
    s
}

fn capacity_row(p: &ValuePage, label: &str, c: &Capacity, prefix: &str) -> String {
    format!(
        "<tr><th scope=\"row\">{label}</th><td>{prefix}{}</td><td>{prefix}{}</td><td>{prefix}{}</td><td>{prefix}{:.1}</td><td>{prefix}{}</td></tr>",
        fmt_tokens(c.tokens),
        fmt_tokens(c.billable_tokens),
        fmt_int(c.requests.round() as i64),
        c.sessions,
        p.usd(c.estimated_cost_usd)
    )
}

fn windows_table(p: &ValuePage, t: &ToolValue) -> String {
    let mut s = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>Per full window, 0 % to 100 %</caption><thead><tr><th scope=\"col\">Window</th><th scope=\"col\">Tokens</th><th scope=\"col\">Uncached + output</th><th scope=\"col\">Requests</th><th scope=\"col\">Sessions</th><th scope=\"col\">Est. API cost</th></tr></thead><tbody>",
    );
    for w in &t.windows {
        let label = format!(
            "{}, {} of {} runs",
            window_name(w),
            w.runs.len(),
            w.qualifying_runs
        );
        match (&w.per_window, &w.lower_bound) {
            (Some(c), _) => s.push_str(&capacity_row(p, &esc(&label), c, "")),
            (None, Some(b)) => s.push_str(&capacity_row(
                p,
                &format!("{}, never exhausted", esc(&window_name(w))),
                b,
                "&ge; ",
            )),
            (None, None) => {
                let _ = write!(
                    s,
                    "<tr><th scope=\"row\">{}</th><td colspan=\"5\" class=\"zero\">no requests</td></tr>",
                    esc(&window_name(w))
                );
            }
        }
    }
    s.push_str("</tbody></table></div>");
    s
}

fn runs_table(p: &ValuePage, w: &WindowEstimate, labels: &[(String, String)]) -> String {
    if w.runs.is_empty() {
        return String::new();
    }
    let mut s = format!(
        "<div class=\"table-wrap\"><table class=\"ledger quiet\"><caption>Runs used for the {} window, largest rise first</caption><thead><tr><th scope=\"col\">Start</th><th scope=\"col\">End</th><th scope=\"col\">From</th><th scope=\"col\">To</th><th scope=\"col\">Tokens</th><th scope=\"col\">Requests</th><th scope=\"col\">Sessions</th><th scope=\"col\">Est. cost</th><th scope=\"col\">Tokens per window</th><th scope=\"col\">Est. cost per window</th></tr></thead><tbody>",
        esc(&w.window)
    );
    for (r, (start, end)) in w.runs.iter().zip(labels) {
        let Run {
            usage, per_window, ..
        } = r;
        let tokens = usage.input_tokens
            + usage.cache_read_input_tokens
            + usage.cache_creation_input_tokens
            + usage.output_tokens;
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}</th><td class=\"l\">{}</td><td>{:.0} %</td><td>{:.0} %</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            esc(start),
            esc(end),
            r.from_percent,
            r.to_percent,
            fmt_tokens(tokens as f64),
            fmt_int(usage.requests),
            fmt_int(r.sessions),
            p.usd(usage.estimated_cost_usd),
            fmt_tokens(per_window.tokens),
            p.usd(per_window.estimated_cost_usd)
        );
    }
    s.push_str("</tbody></table></div>");
    s
}

fn drift_table(p: &ValuePage, w: &WindowEstimate) -> String {
    if w.drift.len() < 2 {
        return String::new();
    }
    let mut s = format!(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>Drift of the {} window, every qualifying run by the month it started in</caption><thead><tr><th scope=\"col\">Month</th><th scope=\"col\">Runs</th><th scope=\"col\">Tokens per window</th><th scope=\"col\">Uncached + output per window</th><th scope=\"col\">Requests per window</th><th scope=\"col\">Sessions per window</th><th scope=\"col\">Est. cost per window</th></tr></thead><tbody>",
        esc(&w.window)
    );
    for d in &w.drift {
        let c = &d.per_window;
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}</th><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{:.1}</td><td>{}</td></tr>",
            esc(&d.month),
            fmt_int(d.runs),
            fmt_tokens(c.tokens),
            fmt_tokens(c.billable_tokens),
            fmt_int(c.requests.round() as i64),
            c.sessions,
            p.usd(c.estimated_cost_usd)
        );
    }
    s.push_str("</tbody></table></div>");
    s
}

/// "Mean of 4 complete months", or the partial ones when none is complete.
fn mean_label(complete: i64, seen: usize) -> String {
    if complete > 0 {
        format!("Mean of {} complete months", fmt_int(complete))
    } else {
        format!("Mean of {} partial months", fmt_int(seen as i64))
    }
}

fn months_table(p: &ValuePage, t: &ToolValue) -> String {
    if t.months.is_empty() {
        return "<p class=\"empty\">No requests recorded for this tool.</p>".into();
    }
    let mut s = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>Estimated API cost by local calendar month against the price</caption><thead><tr><th scope=\"col\">Month</th><th scope=\"col\">Requests</th><th scope=\"col\">Tokens</th><th scope=\"col\">Est. API cost</th><th scope=\"col\">Times the price</th></tr></thead><tbody>",
    );
    for m in &t.months {
        let u = &m.usage;
        let tokens = u.input_tokens
            + u.cache_read_input_tokens
            + u.cache_creation_input_tokens
            + u.output_tokens;
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}{}</th><td>{}</td><td>{}</td><td>{}{}</td><td>{}</td></tr>",
            esc(&m.month),
            if m.complete { "" } else { " (partial)" },
            fmt_int(u.requests),
            fmt_tokens(tokens as f64),
            p.usd(u.estimated_cost_usd),
            if u.unpriced_requests > 0 { "*" } else { "" },
            fmt_x(m.multiple)
        );
    }
    if let (Some(c), Some(x)) = (t.mean_monthly_cost_usd, t.mean_multiple) {
        let _ = write!(
            s,
            "<tr class=\"total\"><th scope=\"row\">{}</th><td></td><td></td><td>{}</td><td>{}</td></tr>",
            mean_label(t.complete_months, t.months.len()),
            p.usd(Some(c)),
            fmt_x(Some(x))
        );
    }
    s.push_str("</tbody></table></div>");
    if t.months.iter().any(|m| m.usage.unpriced_requests > 0) {
        s.push_str("<p class=\"note\">* leaves out requests whose model has no price.</p>");
    }
    s
}

fn tool_section(p: &ValuePage, i: usize, t: &ToolValue) -> String {
    let source = match t.limit_source.as_str() {
        "readings" => format!(
            "Codex reports the used share of both windows with every token count ({} readings). A run is a stretch of rising readings; a drop, a changed reset time, or a gap longer than the window ends it, which keeps resets and reset credits out of the figure. Each run is scaled from its rise to a whole window, and the largest {} rises of at least {:.0} % are averaged.",
            fmt_int(t.windows.iter().map(|w| w.readings).sum()),
            p.report.runs_averaged,
            p.report.min_rise_percent
        ),
        "rejections" => format!(
            "Claude Code only records the moment a window is exhausted ({} rejections). Each run spans from the window's start, its reset time minus the window length, to the first refusal: exactly 0 % to 100 %, so no scaling is needed. A window that was never exhausted shows the busiest window seen as a lower bound.",
            fmt_int(t.windows.iter().map(|w| w.readings).sum())
        ),
        _ => "This harness records no limit readings, so the window capacity cannot be measured; the busiest window seen is shown as a lower bound and the monthly comparison stands on its own.".into(),
    };
    let mut body = format!(
        "<p class=\"note\">{} at {}. {}</p>",
        esc(&t.subscription.name),
        price(t, p),
        source
    );
    body.push_str(&windows_table(p, t));
    body.push_str("<div class=\"stack\">");
    for (j, w) in t.windows.iter().enumerate() {
        body.push_str(&drift_table(p, w));
        body.push_str(&runs_table(
            p,
            w,
            p.run_labels
                .get(i)
                .and_then(|t| t.get(j))
                .map_or(&[][..], |v| v.as_slice()),
        ));
    }
    body.push_str("</div>");
    body.push_str(&months_table(p, t));
    section(
        &format!("value-{}", t.tool),
        &format!("{} on {}", t.tool, t.subscription.name),
        "",
        &body,
    )
}

pub fn render(p: &ValuePage) -> String {
    let mut s = String::with_capacity(64 * 1024);
    s.push_str("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n");
    s.push_str("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n");
    s.push_str("<meta name=\"color-scheme\" content=\"light dark\">\n");
    s.push_str("<meta name=\"referrer\" content=\"no-referrer\">\n");
    s.push_str("<title>Subscription value</title>\n<style>\n");
    s.push_str(CSS);
    s.push_str("</style>\n</head>\n<body>\n<header>\n<h1>Subscription value</h1>\n");
    let provenance = if p.facts.requests == 0 {
        "The store is empty.".to_string()
    } else {
        format!(
            "{} sessions and {} requests. Store read {}, local time {}.",
            fmt_int(p.facts.sessions),
            fmt_int(p.facts.requests),
            esc(&p.now_label),
            esc(&p.tz_label)
        )
    };
    let _ = writeln!(s, "<p class=\"provenance\">{provenance}</p>");
    if p.audience.is_public() {
        let _ = writeln!(
            s,
            "<p class=\"note\">De-identified export. Generated {}. Window: all time. Dollar figures are rounded to the nearest dollar.</p>",
            esc(&p.now_label)
        );
    }
    let _ = writeln!(
        s,
        "<nav class=\"controls\" aria-label=\"Pages\"><p class=\"group\"><span class=\"group-name\">Page</span> <a href=\"{}\">usage habits</a> <a href=\"{}\" aria-current=\"page\">subscription value</a></p></nav>",
        index_href(p),
        self_href(p)
    );
    s.push_str("</header>\n<main>\n");
    let body = if p.facts.requests == 0 {
        "<p class=\"empty\">No usage recorded yet. Run <code>usage-habit-report scan</code> to read your logs.</p>".to_string()
    } else {
        summary(p)
    };
    s.push_str(&section(
        "value",
        "What each plan buys",
        "Estimated API cost is what the same requests would cost at list prices, so \"times the price\" is how much more the plan delivers than it costs. Per-window figures are usage scaled to one whole limit window; a session window is 5 hours and a week is 168. Figures match <code>query value</code>. The mean skips the current month and the first month seen.",
        &body,
    ));
    for (i, t) in p.report.tools.iter().enumerate() {
        s.push_str(&tool_section(p, i, t));
    }
    s.push_str("</main>\n<footer>\n<p>");
    let _ = write!(
        s,
        "Costs are estimates at LiteLLM list prices{}. Subscription prices come from <code>[subscriptions]</code> in config.toml, or the built-in defaults. Data loaded in {:.0} ms.",
        match &p.rates_fetched_at {
            Some(t) => format!(", rates fetched {}", esc(t)),
            None => String::new(),
        },
        p.load_ms
    );
    s.push_str("</p>\n</footer>\n</body>\n</html>\n");
    s
}
