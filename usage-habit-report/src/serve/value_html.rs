//! The secondary page: what each subscription buys. Same frame, tokens and
//! helpers as the main dashboard (`html`), with the figures of `value`.
//! Findings and a summary ledger first, one section per tool, then what the
//! figures rest on; see docs/design.md, "Subscription value page".

use super::data::StoreFacts;
use super::html::{Audience, CSS, esc, fmt_int, section, units};
use crate::value::{
    self, Capacity, Finding, MonthValue, Run, ToolValue, Utilization, ValueReport, WindowEstimate,
};
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
        fn r(c: &mut Option<f64>) {
            *c = c.map(f64::round);
        }
        fn cap(c: &mut Capacity) {
            r(&mut c.estimated_cost_usd);
            r(&mut c.reference_cost_usd);
        }
        r(&mut self.report.combined.pace_monthly_cost_usd);
        for t in &mut self.report.tools {
            r(&mut t.mean_monthly_cost_usd);
            if let Some(p) = &mut t.pace {
                r(&mut p.monthly_cost_usd);
            }
            if let Some(c) = &mut t.credits {
                r(&mut c.credit_funded.estimated_cost_usd);
            }
            for w in &mut t.windows {
                for run in &mut w.runs {
                    r(&mut run.usage.estimated_cost_usd);
                    cap(&mut run.per_window);
                }
                for c in [&mut w.per_window, &mut w.lower_bound]
                    .into_iter()
                    .flatten()
                {
                    cap(c);
                }
                for d in &mut w.drift {
                    cap(&mut d.per_window);
                }
                if let Some(s) = &mut w.spread {
                    r(&mut s.cost_min);
                    r(&mut s.cost_median);
                    r(&mut s.cost_max);
                }
            }
            for m in &mut t.months {
                r(&mut m.usage.estimated_cost_usd);
                r(&mut m.projected_cost_usd);
                r(&mut m.credit_funded_cost_usd);
            }
        }
    }

    /// "$1,305" in the public export, "$1,305.27" locally.
    pub(super) fn usd(&self, cost: Option<f64>) -> String {
        let Some(c) = cost else {
            return "-".into();
        };
        if self.audience.is_public() {
            return value::dollars(c);
        }
        let cents = (c.abs() * 100.0).round() as i64;
        format!(
            "{}${}.{:02}",
            if c < 0.0 && cents > 0 { "-" } else { "" },
            fmt_int(cents / 100),
            cents % 100
        )
    }
}

/// "3.63B" for billions, "12.3M" for millions, plain thousands below that.
fn fmt_tokens(n: f64) -> String {
    if n >= 1e9 {
        format!("{:.2}B", n / 1e9)
    } else if n >= 1e6 {
        format!("{:.1}M", n / 1e6)
    } else {
        fmt_int(n.round() as i64)
    }
}

fn fmt_x(x: Option<f64>) -> String {
    x.map_or("-".into(), |x| format!("{x:.1}x"))
}

fn fmt_pct(x: Option<f64>) -> String {
    x.map_or("-".into(), |x| format!("{x:.0} %"))
}

fn tool_name(tool: &str) -> String {
    format!(
        "<span class=\"tool\" data-tool=\"{0}\">{0}</span>",
        esc(tool)
    )
}

/// A count bar: up to 20 filled units scaled to `max`, in the tool's hue.
fn bar(v: Option<f64>, max: f64, tool: &str) -> String {
    match v {
        Some(v) if v > 0.0 && max > 0.0 => {
            let n = ((v / max) * 20.0).round().clamp(1.0, 20.0) as usize;
            units(n, 20, false, Some(tool))
        }
        _ => String::new(),
    }
}

/// A share bar: a percentage as 20 units of 5 %, with the unfilled track.
fn share_bar(pct: Option<f64>, tool: &str) -> String {
    match pct {
        Some(p) => {
            let n = (p / 5.0).round().clamp(0.0, 20.0) as usize;
            units(n.max(usize::from(p > 0.0)), 20, true, Some(tool))
        }
        None => String::new(),
    }
}

fn token_total(u: &crate::query::Tokens) -> f64 {
    (u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens + u.output_tokens)
        as f64
}

fn price(t: &ToolValue, p: &ValuePage) -> String {
    match t.subscription.monthly_usd {
        Some(c) => format!("{} a month", p.usd(Some(c))),
        None => "no price set".into(),
    }
}

/// "5 h", "weekly", "Fable pool, weekly".
fn window_name(w: &WindowEstimate) -> String {
    let base = if w.window == "weekly" {
        "weekly".to_string()
    } else {
        format!("{} h", w.window_hours)
    };
    match &w.pool {
        Some(pool) => format!("{pool} pool, {base}"),
        None => base,
    }
}

/// "Weekly" for a caption that opens with the window's name.
fn capitalised(s: &str) -> String {
    let mut c = s.chars();
    c.next()
        .map_or(String::new(), |f| f.to_uppercase().chain(c).collect())
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

/// A finding, with a leading tool name set in the tool's hue.
fn finding(text: &str, tools: &[ToolValue]) -> String {
    for t in tools {
        if let Some(rest) = text.strip_prefix(t.tool.as_str())
            && rest.starts_with([' ', ':'])
        {
            return format!("<p>{}{}</p>", tool_name(&t.tool), esc(rest));
        }
    }
    format!("<p>{}</p>", esc(text))
}

fn summary(p: &ValuePage) -> String {
    let r = &p.report;
    let mut s = String::from("<div class=\"facts\">");
    // The combined headline, pools and live limits lead; each tool's own
    // headline repeats the table below, and the rest sit by their tables.
    for (kind, f) in value::tagged_findings(r) {
        let per_tool = r
            .tools
            .iter()
            .any(|t| f.starts_with(&format!("{}:", t.tool)));
        if matches!(kind, Finding::Pool | Finding::Now) || (kind == Finding::Headline && !per_tool)
        {
            s.push_str(&finding(&f, &r.tools));
        }
    }
    s.push_str("</div>");
    s.push_str(&super::value_svg::value_squares(p));
    let _ = write!(
        s,
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>Last {} days</caption><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\" class=\"l\">Plan</th><th scope=\"col\">Price</th><th scope=\"col\">Est. cost a month</th><th scope=\"col\">Times the price</th><th scope=\"col\">Full 5 h</th><th scope=\"col\">Full week</th><th scope=\"col\">Days</th></tr></thead><tbody>",
        r.pace_days
    );
    for t in &r.tools {
        let cell = |w: Option<&WindowEstimate>| match w {
            Some(w) => match (&w.per_window, &w.lower_bound) {
                (Some(c), _) => p.usd(c.estimated_cost_usd),
                (None, Some(b)) => format!("&ge; {}", p.usd(b.estimated_cost_usd)),
                (None, None) => "-".into(),
            },
            None => "-".into(),
        };
        let pace = t.pace.as_ref();
        let multiple = pace.and_then(|p| p.multiple);
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}</th><td class=\"l\">{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            tool_name(&t.tool),
            esc(&t.subscription.name),
            p.usd(t.subscription.monthly_usd),
            p.usd(pace.and_then(|p| p.monthly_cost_usd)),
            fmt_x(multiple),
            cell(value::main_window(t, "session")),
            cell(value::main_window(t, "weekly")),
            pace.map_or("-".into(), |p| format!("{:.0}", p.days)),
        );
    }
    let c = &r.combined;
    let _ = write!(
        s,
        "<tr class=\"total\"><th scope=\"row\">All plans</th><td class=\"l\">{} plans</td><td>{}</td><td>{}</td><td>{}</td><td></td><td></td><td></td></tr>",
        c.plans,
        p.usd(Some(c.monthly_usd)),
        p.usd(c.pace_monthly_cost_usd),
        fmt_x(c.pace_multiple)
    );
    s.push_str("</tbody></table></div>");
    if r.tools.iter().flat_map(|t| &t.windows).any(lower_bound) {
        s.push_str(LOWER_BOUND_NOTE);
    }
    s
}

fn lower_bound(w: &WindowEstimate) -> bool {
    w.per_window.is_none() && w.lower_bound.is_some()
}

const LOWER_BOUND_NOTE: &str = "<p class=\"note\">&ge; marks a window never seen full: the busiest window of that length, a lower bound.</p>";

fn latest_cell(w: &WindowEstimate) -> String {
    match &w.latest {
        Some(l) => format!("{:.0} % on {}", l.used_percent, esc(value::date_of(&l.at))),
        None => "-".into(),
    }
}

fn windows_table(p: &ValuePage, t: &ToolValue) -> String {
    let mut s = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>Per full window</caption><thead><tr><th scope=\"col\">Window</th><th scope=\"col\">Runs</th><th scope=\"col\">Tokens</th><th scope=\"col\">Requests</th><th scope=\"col\">Sessions</th><th scope=\"col\">Est. cost</th><th scope=\"col\">Range</th><th scope=\"col\">Last reading</th></tr></thead><tbody>",
    );
    for w in &t.windows {
        let row = |runs: String, c: &Capacity, prefix: &str, range: String| {
            format!(
                "<tr><th scope=\"row\">{}</th><td>{runs}</td><td>{prefix}{}</td><td>{prefix}{}</td><td>{prefix}{:.1}</td><td>{prefix}{}</td><td>{range}</td><td>{}</td></tr>",
                esc(&window_name(w)),
                fmt_tokens(c.tokens),
                fmt_int(c.requests.round() as i64),
                c.sessions,
                p.usd(c.estimated_cost_usd),
                latest_cell(w)
            )
        };
        match (&w.per_window, &w.lower_bound) {
            (Some(c), _) => {
                let range = match w
                    .spread
                    .as_ref()
                    .and_then(|s| Some((s.cost_min?, s.cost_max?)))
                {
                    Some((lo, hi)) if w.runs.len() > 1 => {
                        format!("{} to {}", p.usd(Some(lo)), p.usd(Some(hi)))
                    }
                    _ => "-".into(),
                };
                s.push_str(&row(
                    format!("{} of {}", w.runs.len(), w.qualifying_runs),
                    c,
                    "",
                    range,
                ));
            }
            (None, Some(b)) => s.push_str(&row("busiest".into(), b, "&ge; ", "-".into())),
            (None, None) => {
                let _ = write!(
                    s,
                    "<tr><th scope=\"row\">{}</th><td colspan=\"6\" class=\"zero\">no requests</td><td>{}</td></tr>",
                    esc(&window_name(w)),
                    latest_cell(w)
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
        "<div class=\"table-wrap\"><table class=\"ledger quiet\"><caption>{} window</caption><thead><tr><th scope=\"col\">Start</th><th scope=\"col\">End</th><th scope=\"col\">From</th><th scope=\"col\">To</th><th scope=\"col\">Tokens</th><th scope=\"col\">Est. cost</th><th scope=\"col\">Per full window</th></tr></thead><tbody>",
        esc(&capitalised(&window_name(w)))
    );
    for (r, (start, end)) in w.runs.iter().zip(labels) {
        let Run {
            usage, per_window, ..
        } = r;
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}</th><td class=\"l\">{}</td><td>{:.0} %</td><td>{:.0} %</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            esc(start),
            esc(end),
            r.from_percent,
            r.to_percent,
            fmt_tokens(token_total(usage)),
            p.usd(usage.estimated_cost_usd),
            p.usd(per_window.estimated_cost_usd)
        );
    }
    s.push_str("</tbody></table></div>");
    s
}

fn drift_table(p: &ValuePage, t: &ToolValue, w: &WindowEstimate) -> String {
    if w.drift.len() < 2 {
        return String::new();
    }
    let mut s = String::new();
    if let Some(d) = value::drift_sentence(t, w) {
        let _ = write!(s, "<p>{}</p>", esc(&d));
    }
    let reference = esc(t.reference_model.as_deref().unwrap_or("one model's"));
    let max = w
        .drift
        .iter()
        .flat_map(|d| {
            [
                d.per_window.estimated_cost_usd,
                d.per_window.reference_cost_usd,
            ]
        })
        .flatten()
        .fold(0.0, f64::max);
    let _ = write!(
        s,
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>{} window by month</caption><thead><tr><th scope=\"col\">Month</th><th scope=\"col\">Runs</th><th scope=\"col\" class=\"l\">Main model</th><th scope=\"col\">Tokens</th><th scope=\"col\">Est. cost</th><th scope=\"col\" class=\"l\"><span class=\"sr\">Est. cost, as a bar</span></th><th scope=\"col\">At {reference} rates</th><th scope=\"col\" class=\"l\"><span class=\"sr\">At {reference} rates, as a bar</span></th></tr></thead><tbody>",
        esc(&capitalised(&window_name(w))),
    );
    for d in &w.drift {
        let c = &d.per_window;
        let model = match (&d.main_model, d.main_model_share) {
            (Some(m), Some(share)) => format!("{} ({:.0} %)", esc(m), share * 100.0),
            (Some(m), None) => esc(m),
            _ => "-".into(),
        };
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}</th><td>{}</td><td class=\"l\">{}</td><td>{}</td><td>{}</td><td class=\"bar\">{}</td><td>{}</td><td class=\"bar\">{}</td></tr>",
            esc(&d.month),
            fmt_int(d.runs),
            model,
            fmt_tokens(c.tokens),
            p.usd(c.estimated_cost_usd),
            bar(c.estimated_cost_usd, max, &t.tool),
            p.usd(c.reference_cost_usd),
            bar(c.reference_cost_usd, max, &t.tool)
        );
    }
    s.push_str("</tbody></table></div>");
    s
}

fn utilization_table(t: &ToolValue) -> String {
    let windows: Vec<(&WindowEstimate, &Utilization)> = t
        .windows
        .iter()
        .filter(|w| w.pool.is_none())
        .filter_map(|w| Some((w, w.utilization.as_ref()?)))
        .filter(|(_, u)| u.mean_peak_percent.is_some() || u.cut_short > 0)
        .collect();
    let mut s = String::new();
    if windows.is_empty() {
        return s;
    }
    s.push_str("<div class=\"table-wrap\"><table class=\"ledger\"><caption>Closed windows</caption><thead><tr><th scope=\"col\">Window</th><th scope=\"col\">Closed</th><th scope=\"col\">Hit 100 %</th><th scope=\"col\">Reset early</th><th scope=\"col\">Mean peak of the rest</th><th scope=\"col\" class=\"l\"><span class=\"sr\">Mean peak, as a bar</span></th></tr></thead><tbody>");
    for (w, u) in &windows {
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}</th><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class=\"bar\">{}</td></tr>",
            esc(&window_name(w)),
            fmt_int(u.windows),
            fmt_int(u.exhausted),
            fmt_int(u.cut_short),
            fmt_pct(u.mean_peak_percent),
            share_bar(u.mean_peak_percent, &t.tool)
        );
    }
    s.push_str("</tbody></table></div>");
    let mut months: Vec<&str> = windows
        .iter()
        .flat_map(|(_, u)| u.by_month.iter().map(|m| m.month.as_str()))
        .collect();
    months.sort_unstable();
    months.dedup();
    if months.len() < 2 {
        return s;
    }
    s.push_str("<div class=\"table-wrap\"><table class=\"ledger quiet\"><caption>Closed windows by month</caption><thead><tr><th scope=\"col\">Month</th>");
    for (w, _) in &windows {
        let n = esc(&window_name(w));
        let _ = write!(
            s,
            "<th scope=\"col\">{n}</th><th scope=\"col\">{n} at 100 %</th><th scope=\"col\">{n} mean peak</th>"
        );
    }
    s.push_str("</tr></thead><tbody>");
    for m in months {
        let _ = write!(s, "<tr><th scope=\"row\">{}</th>", esc(m));
        for (_, u) in &windows {
            match u.by_month.iter().find(|x| x.month == m) {
                Some(x) => {
                    let _ = write!(
                        s,
                        "<td>{}</td><td>{}</td><td>{}</td>",
                        fmt_int(x.windows),
                        fmt_int(x.exhausted),
                        fmt_pct(x.mean_peak_percent)
                    );
                }
                None => s.push_str("<td class=\"zero\">0</td><td></td><td></td>"),
            }
        }
        s.push_str("</tr>");
    }
    s.push_str("</tbody></table></div>");
    s
}

fn credits_table(p: &ValuePage, t: &ToolValue) -> String {
    let Some(c) = t
        .credits
        .as_ref()
        .filter(|c| c.spent > 0.5 || c.added > 0.5)
    else {
        return String::new();
    };
    let rows = [
        ("Added", value::grouped(c.added)),
        ("Spent", value::grouped(c.spent)),
        ("at a full window", value::grouped(c.spent_at_limit)),
        ("on mid-window resets", value::grouped(c.spent_near_resets)),
        ("Left", value::grouped(c.last_balance)),
        (
            "Requests paid with credits",
            fmt_int(c.credit_funded.requests),
        ),
        (
            "Their est. cost, left out of the plan's value",
            p.usd(c.credit_funded.estimated_cost_usd),
        ),
    ];
    let mut s = String::new();
    let _ = write!(
        s,
        "<div class=\"table-wrap\"><table class=\"ledger quiet\"><caption>Credits from {} to {}</caption><thead><tr><th scope=\"col\">Credits</th><th scope=\"col\">Amount</th></tr></thead><tbody>",
        esc(value::date_of(&c.first_at)),
        esc(value::date_of(&c.last_at))
    );
    for (label, v) in rows {
        let _ = write!(s, "<tr><th scope=\"row\">{label}</th><td>{v}</td></tr>");
    }
    s.push_str("</tbody></table></div>");
    s
}

/// "Mean of 4 complete months", or the partial ones when none is complete.
fn mean_label(complete: i64, seen: usize) -> String {
    if complete > 0 {
        format!("Mean, {} complete months", fmt_int(complete))
    } else {
        let n = seen as i64;
        format!(
            "Mean, {} partial month{}",
            fmt_int(n),
            if n == 1 { "" } else { "s" }
        )
    }
}

fn months_table(p: &ValuePage, t: &ToolValue) -> String {
    if t.months.is_empty() {
        return "<p class=\"empty\">No requests recorded for this tool.</p>".into();
    }
    let credits = t
        .months
        .iter()
        .any(|m| m.credit_funded_cost_usd.is_some_and(|c| c > 0.0));
    let max = t
        .months
        .iter()
        .filter_map(|m| m.multiple)
        .chain(t.pace.as_ref().and_then(|p| p.multiple))
        .fold(0.0, f64::max);
    let mut s = format!(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>By month</caption><thead><tr><th scope=\"col\">Month</th><th scope=\"col\">Days</th><th scope=\"col\">Tokens</th><th scope=\"col\">Est. cost</th>{}<th scope=\"col\">Times the price</th><th scope=\"col\" class=\"l\"><span class=\"sr\">Times the price, as a bar</span></th><th scope=\"col\">Whole-month pace</th></tr></thead><tbody>",
        if credits {
            "<th scope=\"col\">Paid with credits</th>"
        } else {
            ""
        }
    );
    let credit_cell = |m: Option<&MonthValue>| {
        if !credits {
            return String::new();
        }
        match m
            .and_then(|m| m.credit_funded_cost_usd)
            .filter(|c| *c > 0.0)
        {
            Some(c) => format!("<td>{}</td>", p.usd(Some(c))),
            None => "<td></td>".into(),
        }
    };
    for m in &t.months {
        let u = &m.usage;
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}{}</th><td>{:.0}</td><td>{}</td><td>{}{}</td>{}<td>{}</td><td class=\"bar\">{}</td><td>{}</td></tr>",
            esc(&m.month),
            if m.complete { "" } else { " (partial)" },
            m.days_covered,
            fmt_tokens(token_total(u)),
            p.usd(u.estimated_cost_usd),
            if u.unpriced_requests > 0 { "*" } else { "" },
            credit_cell(Some(m)),
            fmt_x(m.multiple),
            bar(m.multiple, max, &t.tool),
            if m.complete {
                String::new()
            } else {
                p.usd(m.projected_cost_usd)
            }
        );
    }
    if let (Some(c), Some(x)) = (t.mean_monthly_cost_usd, t.mean_multiple)
        && t.months.len() > 1
    {
        let _ = write!(
            s,
            "<tr class=\"total\"><th scope=\"row\">{}</th><td></td><td></td><td>{}</td>{}<td>{}</td><td></td><td></td></tr>",
            mean_label(t.complete_months, t.months.len()),
            p.usd(Some(c)),
            credit_cell(None),
            fmt_x(Some(x))
        );
    }
    if let Some(pace) = &t.pace
        && let (Some(c), Some(x)) = (pace.monthly_cost_usd, pace.multiple)
    {
        let _ = write!(
            s,
            "<tr class=\"total\"><th scope=\"row\">Pace, last {:.0} days</th><td></td><td></td><td>{}</td>{}<td>{}</td><td class=\"bar\">{}</td><td></td></tr>",
            pace.days,
            p.usd(Some(c)),
            credit_cell(None),
            fmt_x(Some(x)),
            bar(Some(x), max, &t.tool)
        );
    }
    s.push_str("</tbody></table></div>");
    if t.months.iter().any(|m| m.usage.unpriced_requests > 0) {
        s.push_str("<p class=\"note\">* leaves out models with no price.</p>");
    }
    s
}

/// "$150.00 a month. Requests 2026-10-13 to 2026-10-23; 25 refusals logged, 180 live readings."
fn source_note(p: &ValuePage, t: &ToolValue) -> String {
    let logged: i64 = t
        .windows
        .iter()
        .filter(|w| w.pool.is_none())
        .map(|w| w.readings - w.live_readings)
        .sum();
    let live: i64 = t.windows.iter().map(|w| w.live_readings).sum();
    let logged = match t.tool.as_str() {
        "codex" => format!("{} logged readings", fmt_int(logged)),
        "claude-code" => format!("{} refusals logged", fmt_int(logged)),
        _ => "nothing logged".into(),
    };
    let span = match (&t.first_request, &t.last_request) {
        (Some(a), Some(b)) => format!(
            " Requests {} to {};",
            esc(value::date_of(a)),
            esc(value::date_of(b))
        ),
        _ => String::new(),
    };
    format!(
        "{}.{span} {logged}, {} live readings.",
        price(t, p),
        fmt_int(live)
    )
}

fn tool_section(p: &ValuePage, i: usize, t: &ToolValue) -> String {
    let mut body = format!("<p class=\"note\">{}</p>", source_note(p, t));
    body.push_str(&windows_table(p, t));
    body.push_str("<div class=\"stack\">");
    for w in t.windows.iter().filter(|w| w.pool.is_none()) {
        body.push_str(&drift_table(p, t, w));
    }
    body.push_str(&utilization_table(t));
    body.push_str(&credits_table(p, t));
    body.push_str(&months_table(p, t));
    let runs: String = t
        .windows
        .iter()
        .enumerate()
        .map(|(j, w)| {
            runs_table(
                p,
                w,
                p.run_labels
                    .get(i)
                    .and_then(|t| t.get(j))
                    .map_or(&[][..], |v| v.as_slice()),
            )
        })
        .collect();
    if !runs.is_empty() {
        let _ = write!(
            body,
            "<details><summary>Runs averaged, largest rise first</summary>{runs}</details>"
        );
    }
    body.push_str("</div>");
    section(
        &format!("value-{}", t.tool),
        &format!("{} on {}", t.tool, t.subscription.name),
        "",
        &body,
    )
}

/// How the figures are made. Always rendered, so its fixed words reach the
/// snapshot's template vocabulary.
fn coverage(p: &ValuePage) -> String {
    let mut s = String::from("<div class=\"coverage\">");
    let _ = write!(
        s,
        "<p>A run is a stretch of rising readings in one window, ended by a reset or a gap. It stops at its first 100 % reading and is scaled to a whole window. The {} largest rises of at least {:.0} % are averaged.</p>",
        p.report.runs_averaged, p.report.min_rise_percent
    );
    s.push_str("<p>Codex logs a reading with every request. Claude Code logs only refusals and Antigravity logs nothing, so their figures come from live readings taken by <code>usage-habit-report limits record</code>.</p>");
    s.push_str("<p>Drift also prices every month at one reference model's rates, to separate a change in work from a change in prices. Means skip the current and first month. Requests paid with credits are left out.</p>");
    s.push_str("</div>");
    s
}

pub fn render(p: &ValuePage) -> String {
    let mut s = String::with_capacity(96 * 1024);
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
        "Est. cost prices each request at API list rates. Times the price is that cost over the plan's price.",
        &body,
    ));
    for (i, t) in p.report.tools.iter().enumerate() {
        s.push_str(&tool_section(p, i, t));
    }
    s.push_str(&section(
        "value-coverage",
        "Coverage and method",
        "",
        &coverage(p),
    ));
    s.push_str("</main>\n<footer>\n<p>");
    let _ = write!(
        s,
        "Estimates at LiteLLM list prices{}. Plan prices from <code>[subscriptions]</code> in config.toml or the defaults. Loaded in {:.0} ms.",
        match &p.rates_fetched_at {
            Some(t) => format!(", rates fetched {}", esc(t)),
            None => String::new(),
        },
        p.load_ms
    );
    s.push_str("</p>\n</footer>\n</body>\n</html>\n");
    s
}
