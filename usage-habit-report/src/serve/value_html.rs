//! The secondary page: what each subscription buys. Same frame, tokens and
//! helpers as the main dashboard (`html`), with the figures of `value`.
//! Findings and a summary ledger first, one section per tool, then what the
//! figures rest on; see docs/design.md, "Subscription value page".

use super::data::StoreFacts;
use super::html::{Audience, CSS, esc, fmt_int, section, units};
use crate::value::{
    self, Capacity, Finding, MonthValue, Run, ToolValue, ValueReport, WindowEstimate,
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
            return NO_DATA.into();
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

/// The mark for a cell with too little data to fill, explained by
/// [`noted`] under its table.
const NO_DATA: &str = "*";

/// `tables` with a "not enough data" note when any cell holds [`NO_DATA`].
fn noted(mut tables: String) -> String {
    if tables.contains("*</td>") {
        tables.push_str("<p class=\"note\">* Not enough data.</p>");
    }
    tables
}

fn fmt_x(x: Option<f64>) -> String {
    x.map_or(NO_DATA.into(), |x| format!("{x:.1}x"))
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

fn token_total(u: &crate::query::Tokens) -> f64 {
    (u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens + u.output_tokens)
        as f64
}

fn price(t: &ToolValue, p: &ValuePage) -> String {
    let sub = &t.subscription;
    match (sub.monthly_usd, sub.paid_monthly_usd) {
        (Some(c), Some(paid)) if (c - paid).abs() >= 0.005 => format!(
            "{} a month at list prices; you pay {}",
            p.usd(Some(c)),
            p.usd(Some(paid))
        ),
        (Some(c), _) => format!("{} a month", p.usd(Some(c))),
        (None, _) => "no price set".into(),
    }
}

/// "ChatGPT Plus, seat 2": how the public page names a plan line. A label
/// or an account never appears there; locally the label follows.
fn plan_name(p: &ValuePage, plan: &value::PlanValue) -> String {
    let mut s = plan.name.clone();
    if let Some(n) = plan.seat {
        let _ = write!(s, ", seat {n}");
    }
    if !p.audience.is_public()
        && let Some(label) = &plan.label
    {
        let _ = write!(s, " ({label})");
    }
    s
}

/// Each plan line with its prices and dates, when a tool has more than one
/// or any line is dated or remote.
fn plans_note(p: &ValuePage, t: &ToolValue) -> String {
    let plans = &t.subscription.plans;
    let plain = |l: &value::PlanValue| l.from.is_none() && l.until.is_none() && !l.remote;
    if plans.len() < 2 && plans.iter().all(plain) {
        return String::new();
    }
    let when = |t: &str| t.replace('T', " ");
    let lines: Vec<String> = plans
        .iter()
        .map(|l| {
            let mut s = format!("{}: {}", plan_name(p, l), p.usd(l.monthly_usd));
            if l.paid_monthly_usd != l.monthly_usd {
                let _ = write!(s, " list, you pay {}", p.usd(l.paid_monthly_usd));
            }
            match (&l.from, &l.until) {
                (Some(a), Some(b)) => {
                    let _ = write!(s, ", {} to {}", when(a), when(b));
                }
                (Some(a), None) => {
                    let _ = write!(s, ", from {}", when(a));
                }
                (None, Some(b)) => {
                    let _ = write!(s, ", until {}", when(b));
                }
                (None, None) => {}
            }
            if l.remote {
                s.push_str(", used on another machine: its price counts here, its use is not in this store");
            }
            s
        })
        .collect();
    format!("<p class=\"note\">Plans: {}.</p>", esc(&lines.join("; ")))
}

/// Seats pooled into a window and readings left out, for one tool.
fn seats_note(t: &ToolValue) -> String {
    let mut s = String::new();
    let seats = t
        .windows
        .iter()
        .filter(|w| w.pool.is_none())
        .map(|w| w.seats)
        .max()
        .unwrap_or(1);
    if seats > 1 {
        let _ = write!(
            s,
            " Readings come from {seats} seats; each seat's windows are measured on their own and their runs averaged together."
        );
    }
    let left: i64 = t.windows.iter().map(|w| w.unattributed_readings).sum();
    if left > 0 {
        let _ = write!(
            s,
            " {} older readings are left out: they name no account, and more than one was in use.",
            fmt_int(left)
        );
    }
    if s.is_empty() {
        s
    } else {
        format!("<p class=\"note\">{}</p>", s.trim_start())
    }
}

/// When a tool's price over the pace's days differs from its price now:
/// that price, and the dates its plans changed within those days.
fn repriced(r: &ValueReport, t: &ToolValue) -> Option<(f64, Vec<String>)> {
    let pace = t.pace.as_ref()?;
    let then = pace.price_usd?;
    if (then - t.subscription.monthly_usd.unwrap_or(0.0)).abs() < 0.005 {
        return None;
    }
    let since = value::days_before(&r.generated_at, pace.days);
    let mut dates: Vec<String> = t
        .subscription
        .plans
        .iter()
        .flat_map(|l| [&l.from, &l.until])
        .flatten()
        .filter(|d| since.as_deref().is_none_or(|s| d.as_str() >= s) && **d <= r.generated_at)
        .map(|d| value::date_of(d).to_owned())
        .collect();
    dates.sort();
    dates.dedup();
    Some((then, dates))
}

/// "5 hr", "weekly", "Fable pool, weekly".
fn window_name(w: &WindowEstimate) -> String {
    let base = if w.window == "weekly" {
        "weekly".to_string()
    } else {
        format!("{} hr", w.window_hours)
    };
    match &w.pool {
        Some(pool) => format!("{pool} pool, {base}"),
        None => base,
    }
}

/// `window_name`, led by the plan's name when the tool has windows of
/// several plans: "Google AI Ultra, weekly".
fn tier_window_name(t: &ToolValue, w: &WindowEstimate) -> String {
    if t.windows.iter().any(|x| x.plan != w.plan) {
        format!("{}, {}", w.plan, window_name(w))
    } else {
        window_name(w)
    }
}

/// "Weekly" for a caption that opens with the window's name.
pub(super) fn capitalised(s: &str) -> String {
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
    let mut s = String::new();
    let alerts: Vec<String> = value::tagged_findings(r)
        .into_iter()
        .filter(|(kind, _)| {
            // Live-limit alerts lead; the accounts finding names accounts, so
            // it stays local. Pool figures appear in the ledger below.
            *kind == Finding::Now || (*kind == Finding::Accounts && !p.audience.is_public())
        })
        .map(|(_, f)| finding(&f, &r.tools))
        .collect();
    if !alerts.is_empty() {
        s.push_str("<div class=\"facts\">");
        for a in alerts {
            s.push_str(&a);
        }
        s.push_str("</div>");
    }
    s.push_str(&super::value_svg::value_squares(p));
    let mut table = String::new();
    let _ = write!(
        table,
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>Last {} days</caption><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\" class=\"l\">Plan</th><th scope=\"col\">Price</th><th scope=\"col\">You pay</th><th scope=\"col\">Est. cost a month</th><th scope=\"col\">Times the price</th><th scope=\"col\">Full 5 hr</th><th scope=\"col\">Full week</th><th scope=\"col\">Days</th></tr></thead><tbody>",
        r.pace_days
    );
    for t in &r.tools {
        let cell = |w: Option<&WindowEstimate>| match w {
            Some(w) => match (&w.per_window, &w.lower_bound) {
                (Some(c), _) => p.usd(c.estimated_cost_usd),
                (None, Some(b)) => format!("&ge; {}", p.usd(b.estimated_cost_usd)),
                (None, None) => NO_DATA.into(),
            },
            None => NO_DATA.into(),
        };
        let pace = t.pace.as_ref();
        let multiple = pace.and_then(|p| p.multiple);
        let mark = if repriced(r, t).is_some() { "*" } else { "" };
        let _ = write!(
            table,
            "<tr><th scope=\"row\">{}</th><td class=\"l\">{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}{mark}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            tool_name(&t.tool),
            esc(&t.subscription.name),
            p.usd(t.subscription.monthly_usd),
            p.usd(t.subscription.paid_monthly_usd),
            p.usd(pace.and_then(|p| p.monthly_cost_usd)),
            fmt_x(multiple),
            cell(value::main_window(t, "session")),
            cell(value::main_window(t, "weekly")),
            pace.map_or(NO_DATA.into(), |p| format!("{:.0}", p.days)),
        );
    }
    let c = &r.combined;
    let _ = write!(
        table,
        "<tr class=\"total\"><th scope=\"row\">All plans</th><td class=\"l\">{} plans</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td></td><td></td><td></td></tr>",
        c.plans,
        p.usd(Some(c.monthly_usd)),
        p.usd(Some(c.paid_monthly_usd)),
        p.usd(c.pace_monthly_cost_usd),
        fmt_x(c.pace_multiple)
    );
    table.push_str("</tbody></table></div>");
    s.push_str(&noted(table));
    let changed: Vec<String> = r
        .tools
        .iter()
        .filter_map(|t| {
            let (then, dates) = repriced(r, t)?;
            let mut s = format!("{} {}", t.tool, p.usd(Some(then)));
            if !dates.is_empty() {
                let _ = write!(s, ", plans changed {}", dates.join(", "));
            }
            Some(s)
        })
        .collect();
    if !changed.is_empty() {
        let _ = write!(
            s,
            "<p class=\"note\">* Times the price uses the list price over the last {} days, each plan weighted by the days it ran: {}.</p>",
            r.pace_days,
            esc(&changed.join("; "))
        );
    }
    if r.tools.iter().flat_map(|t| &t.windows).any(lower_bound) {
        s.push_str(LOWER_BOUND_NOTE);
    }
    s.push_str(&model_pools(p));
    s.push_str(&session_shares(&r.tools));
    s
}

/// Per-model quota pools with measured capacity, grouped by tool, tier and pool.
fn model_pools(p: &ValuePage) -> String {
    type PoolRow<'a> = (
        &'a str,
        usize,
        &'a str,
        &'a str,
        Option<&'a Capacity>,
        Option<&'a Capacity>,
    );
    let mut rows: Vec<PoolRow<'_>> = Vec::new();
    for t in &p.report.tools {
        let mut tier_idx = 0usize;
        for (i, w) in t.windows.iter().enumerate() {
            if i > 0 && w.pool.is_none() && w.window == "session" {
                tier_idx += 1;
            }
            let (Some(pool), Some(cap)) = (w.pool.as_deref(), w.per_window.as_ref()) else {
                continue;
            };
            let entry = match rows
                .iter_mut()
                .find(|r| r.0 == t.tool && r.1 == tier_idx && r.2 == w.plan && r.3 == pool)
            {
                Some(e) => e,
                None => {
                    rows.push((t.tool.as_str(), tier_idx, w.plan.as_str(), pool, None, None));
                    rows.last_mut().unwrap()
                }
            };
            if w.window == "weekly" {
                entry.5 = Some(cap);
            } else {
                entry.4 = Some(cap);
            }
        }
    }
    if rows.is_empty() {
        return String::new();
    }
    let mut table = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>Model pools</caption><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\" class=\"l\">Plan</th><th scope=\"col\" class=\"l\">Pool</th><th scope=\"col\">Full 5 hr</th><th scope=\"col\">Full week</th></tr></thead><tbody>",
    );
    let cell = |cap: Option<&Capacity>| match cap {
        Some(c) => format!("<td>{}</td>", p.usd(c.estimated_cost_usd)),
        None => "<td></td>".into(),
    };
    for (tool, _, plan, pool, session, weekly) in rows {
        let _ = write!(
            table,
            "<tr><th scope=\"row\">{}</th><td class=\"l\">{}</td><td class=\"l\">{}</td>{}{}</tr>",
            tool_name(tool),
            esc(plan),
            esc(pool),
            cell(session),
            cell(weekly),
        );
    }
    table.push_str("</tbody></table></div>");
    noted(table)
}

/// Weekly limit over session limit: how many full sessions one week holds.
fn session_shares(tools: &[value::ToolValue]) -> String {
    let rows: Vec<_> = tools
        .iter()
        .flat_map(|t| {
            t.session_shares
                .iter()
                .filter(|s| s.windows > 0)
                .map(move |s| (t, s))
        })
        .collect();
    if rows.is_empty() {
        return String::new();
    }
    let mut s = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>Weekly limit / session limit</caption><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\" class=\"l\">Limit</th><th scope=\"col\">Sessions a week</th><th scope=\"col\">One session uses</th><th scope=\"col\">Sessions measured</th></tr></thead><tbody>",
    );
    let mut pool_only = false;
    for (t, sh) in rows {
        let multi_plan = t.windows.iter().any(|w| w.plan != sh.plan);
        let base = match (&sh.session_pool, &sh.weekly_pool) {
            (Some(p), _) => format!("{p} pool"),
            (None, Some(w)) => {
                pool_only = true;
                format!("{w} weekly, {w} use only")
            }
            (None, None) => "Plan".to_owned(),
        };
        let limit = if multi_plan && (sh.session_pool.is_some() || sh.weekly_pool.is_some()) {
            format!("{}, {base}", sh.plan)
        } else {
            base
        };
        let (ratio, share) = match sh.weekly_percent.filter(|v| *v > 0.0) {
            Some(v) => (format!("{:.1}", 100.0 / v), format!("{v:.0} % of the week")),
            None => (NO_DATA.into(), NO_DATA.into()),
        };
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}</th><td class=\"l\">{}</td><td>{ratio}</td><td>{share}</td><td>{}</td></tr>",
            tool_name(&t.tool),
            esc(&limit),
            sh.windows
        );
    }
    s.push_str("</tbody></table></div>");
    let mut s = noted(s);
    let _ = write!(
        s,
        "<p class=\"note\">Measured from session and weekly readings taken together, over 5-hour sessions that used at least {:.0} % of their limit. {}</p>",
        value::MIN_RISE_PERCENT,
        if pool_only {
            " A pool with only a weekly limit shares the plan's session limit with other models, so each session counts only the pool's share of its cost."
        } else {
            ""
        }
    );
    s
}

fn lower_bound(w: &WindowEstimate) -> bool {
    w.per_window.is_none() && w.lower_bound.is_some()
}

const LOWER_BOUND_NOTE: &str = "<p class=\"note\">&ge; marks a window never seen full: the busiest window of that length, a lower bound.</p>";

fn runs_table(
    p: &ValuePage,
    t: &ToolValue,
    w: &WindowEstimate,
    labels: &[(String, String)],
) -> String {
    if w.runs.is_empty() {
        return String::new();
    }
    let mut s = format!(
        "<div class=\"table-wrap\"><table class=\"ledger quiet\"><caption>{} window</caption><thead><tr><th scope=\"col\">Start</th><th scope=\"col\">End</th><th scope=\"col\">From</th><th scope=\"col\">To</th><th scope=\"col\">Tokens</th><th scope=\"col\">Est. cost</th><th scope=\"col\">Per full window</th></tr></thead><tbody>",
        esc(&capitalised(&tier_window_name(t, w)))
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

/// The drift of every plan window in one figure, then their value by
/// model in another.
fn drift_section(t: &ToolValue) -> String {
    let panels: Vec<_> = t
        .windows
        .iter()
        .filter(|w| w.pool.is_none())
        .map(|w| super::value_svg::WindowPanel {
            window: w,
            name: tier_window_name(t, w),
            sentence: value::drift_sentence(t, w),
        })
        .collect();
    super::value_svg::drift_chart(t, &panels) + &super::value_svg::model_chart(t, &panels)
}

fn credits_note(p: &ValuePage, t: &ToolValue) -> String {
    if !matches!(p.audience, Audience::Private) {
        return String::new();
    }
    let Some(c) = t.credits.as_ref().filter(|c| c.spent > 0.5) else {
        return String::new();
    };
    let value = c.credit_funded.estimated_cost_usd.filter(|v| *v > 0.0);
    format!(
        "<p>Credits: {} of {} spent, {} at a full window and {} on early resets, {} left.{}</p>",
        value::grouped(c.spent),
        value::grouped(c.added.max(c.spent)),
        value::grouped(c.spent_at_limit),
        value::grouped(c.spent_near_resets),
        value::grouped(c.last_balance),
        value.map_or(String::new(), |v| format!(
            " The use they paid for is worth est. {} at list prices, {:.1}¢ a credit.",
            p.usd(Some(v)),
            100.0 * v / c.spent
        )),
    )
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
        .fold(0.0, f64::max);
    // The month's own price, when any month's differs from the price now.
    let priced = t
        .months
        .iter()
        .any(|m| m.price_usd != t.subscription.monthly_usd || m.paid_usd != m.price_usd);
    let mut s = format!(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>By month</caption><thead><tr><th scope=\"col\">Month</th><th scope=\"col\">Days</th><th scope=\"col\">Tokens</th><th scope=\"col\">Est. cost</th>{}{}<th scope=\"col\">Times the price</th><th scope=\"col\" class=\"l\"><span class=\"sr\">Times the price, as a bar</span></th><th scope=\"col\">Whole-month pace</th></tr></thead><tbody>",
        if credits {
            "<th scope=\"col\">Paid with credits</th>"
        } else {
            ""
        },
        if priced {
            "<th scope=\"col\">Price</th><th scope=\"col\">You pay</th>"
        } else {
            ""
        }
    );
    let price_cells = |m: Option<&MonthValue>| match (priced, m) {
        (false, _) => String::new(),
        (true, Some(m)) => format!(
            "<td>{}</td><td>{}</td>",
            p.usd(m.price_usd),
            p.usd(m.paid_usd)
        ),
        (true, None) => "<td></td><td></td>".into(),
    };
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
            "<tr><th scope=\"row\">{}{}</th><td>{:.0}</td><td>{}</td><td>{}{}</td>{}{}<td>{}</td><td class=\"bar\">{}</td><td>{}</td></tr>",
            esc(&m.month),
            if m.complete { "" } else { " (partial)" },
            m.days_covered,
            fmt_tokens(token_total(u)),
            p.usd(u.estimated_cost_usd),
            if u.unpriced_requests > 0 {
                "&dagger;"
            } else {
                ""
            },
            credit_cell(Some(m)),
            price_cells(Some(m)),
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
            "<tr class=\"total\"><th scope=\"row\">{}</th><td></td><td></td><td>{}</td>{}{}<td>{}</td><td></td><td></td></tr>",
            mean_label(t.complete_months, t.months.len()),
            p.usd(Some(c)),
            credit_cell(None),
            price_cells(None),
            fmt_x(Some(x))
        );
    }
    s.push_str("</tbody></table></div>");
    if t.months.iter().any(|m| m.usage.unpriced_requests > 0) {
        s.push_str("<p class=\"note\">&dagger; leaves out models with no price.</p>");
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
    body.push_str(&plans_note(p, t));
    body.push_str(&seats_note(t));
    body.push_str("<div class=\"stack\">");
    body.push_str(&drift_section(t));
    body.push_str(&credits_note(p, t));
    body.push_str(&noted(months_table(p, t)));
    let runs: String = t
        .windows
        .iter()
        .enumerate()
        .map(|(j, w)| {
            runs_table(
                p,
                t,
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
            "<details><summary>Runs averaged, largest rise first</summary>{}</details>",
            noted(runs)
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
        "Est. cost prices each request at API list rates. Times the price is the cost of use while a plan ran, over the plan's price.",
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
        "Estimates at LiteLLM list prices{}. Plan prices from <code>[subscriptions]</code> in config.toml or the defaults. Data loaded in {:.0} ms.",
        match &p.rates_fetched_at {
            Some(t) => format!(", rates fetched {}", esc(t)),
            None => String::new(),
        },
        p.load_ms
    );
    s.push_str("</p>\n</footer>\n</body>\n</html>\n");
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::{Combined, Latest, SessionShare, Subscription};

    fn page(tools: Vec<ToolValue>, audience: Audience) -> ValuePage {
        ValuePage {
            audience,
            now_label: "Wed 18 Nov 2026, 20:00".into(),
            tz_label: "UTC-05:00".into(),
            facts: StoreFacts {
                sessions: 10,
                requests: 100,
                first_ts_ms: None,
                last_ts_ms: None,
            },
            report: ValueReport {
                generated_at: "2026-11-19T01:00:00Z".into(),
                min_rise_percent: 20.0,
                runs_averaged: 10,
                tools,
                pace_days: 28,
                combined: Combined {
                    plans: 1,
                    monthly_usd: 200.0,
                    paid_monthly_usd: 0.0,
                    pace_plans: 1,
                    pace_price_usd: Some(200.0),
                    pace_paid_usd: Some(0.0),
                    pace_monthly_cost_usd: Some(500.0),
                    pace_multiple: Some(2.5),
                    pace_paid_multiple: None,
                },
            },
            run_labels: vec![],
            rates_fetched_at: None,
            load_ms: 1.0,
        }
    }

    fn cap(cost: f64) -> Capacity {
        Capacity {
            tokens: 1_000_000.0,
            estimated_cost_usd: Some(cost),
            ..Default::default()
        }
    }

    #[test]
    fn pools_render_as_a_ledger_without_facts_prose_and_empty_shares_drop() {
        let pool_win = |plan: &str, pool: &str, window: &str, cost: Option<f64>| WindowEstimate {
            plan: plan.into(),
            pool: Some(pool.into()),
            window: window.into(),
            window_hours: if window == "weekly" { 168 } else { 5 },
            per_window: cost.map(cap),
            ..Default::default()
        };
        let ag = ToolValue {
            tool: "antigravity".into(),
            subscription: Subscription {
                name: "Tier <Plus> + Pro".into(),
                monthly_usd: Some(219.99),
                paid_monthly_usd: Some(19.99),
                plans: vec![],
            },
            windows: vec![
                WindowEstimate {
                    plan: "Tier <Plus>".into(),
                    window: "session".into(),
                    window_hours: 5,
                    ..Default::default()
                },
                pool_win("Tier <Plus>", "Pool <A>", "session", Some(204.0)),
                pool_win("Tier <Plus>", "Pool <A>", "weekly", Some(718.0)),
                pool_win("Pro", "Argon", "weekly", Some(14.0)),
            ],
            session_shares: vec![
                SessionShare {
                    plan: "Pro".into(),
                    session_pool: Some("Gemini".into()),
                    weekly_pool: Some("Gemini".into()),
                    windows: 0,
                    weekly_percent: None,
                },
                SessionShare {
                    plan: "Tier <Plus>".into(),
                    session_pool: Some("Pool <A>".into()),
                    weekly_pool: Some("Pool <A>".into()),
                    windows: 4,
                    weekly_percent: Some(10.0),
                },
            ],
            ..Default::default()
        };
        let p = page(vec![ag], Audience::Private);
        let html = summary(&p);

        // No live/account alerts -> no `.facts` prose block.
        assert!(!html.contains("<div class=\"facts\">"), "{html}");
        // Model pools ledger groups session + weekly per (tool, plan, pool) and escapes HTML.
        assert!(html.contains("<caption>Model pools</caption>"), "{html}");
        assert!(
            html.contains("<td class=\"l\">Tier &lt;Plus&gt;</td><td class=\"l\">Pool &lt;A&gt;</td><td>$204.00</td><td>$718.00</td>"),
            "{html}"
        );
        // Weekly-only pool leaves Full 5 hr empty (not `*`) without triggering false "Not enough data".
        assert!(
            html.contains(
                "<td class=\"l\">Pro</td><td class=\"l\">Argon</td><td></td><td>$14.00</td>"
            ),
            "{html}"
        );
        // Zero-window session share is omitted; multi-plan pool share carries its plan prefix.
        assert!(!html.contains("Gemini pool"), "{html}");
        assert!(
            html.contains("<td class=\"l\">Tier &lt;Plus&gt;, Pool &lt;A&gt; pool</td>"),
            "{html}"
        );

        // With a live exhausted alert, `.facts` appears and stays free of pool summaries.
        let mut alert_ag = p.report.tools[0].clone();
        alert_ag.windows[0].live_readings = 2;
        alert_ag.windows[0].latest = Some(Latest {
            at: "2026-11-19T00:00:00Z".into(),
            used_percent: 100.0,
            resets_at: Some("2026-11-19T05:00:00Z".into()),
            live: true,
        });
        let alert_html = summary(&page(vec![alert_ag], Audience::Private));
        assert!(alert_html.contains("<div class=\"facts\">"), "{alert_html}");
        assert!(alert_html.contains("100 % used"), "{alert_html}");
        assert!(!alert_html.contains("per 5-hour window"), "{alert_html}");
    }

    #[test]
    fn same_name_tiers_render_separate_model_pool_rows() {
        let pool_win = |plan: &str, window: &str, cost: f64| WindowEstimate {
            plan: plan.into(),
            pool: Some("Gemini".into()),
            window: window.into(),
            window_hours: if window == "weekly" { 168 } else { 5 },
            per_window: Some(cap(cost)),
            ..Default::default()
        };
        let tier_win = |plan: &str, window: &str| WindowEstimate {
            plan: plan.into(),
            window: window.into(),
            window_hours: if window == "weekly" { 168 } else { 5 },
            ..Default::default()
        };
        let ag = ToolValue {
            tool: "antigravity".into(),
            windows: vec![
                tier_win("Google AI", "session"),
                tier_win("Google AI", "weekly"),
                pool_win("Google AI", "session", 10.0),
                tier_win("Google AI", "session"),
                tier_win("Google AI", "weekly"),
                pool_win("Google AI", "weekly", 30.0),
            ],
            ..Default::default()
        };
        let html = summary(&page(vec![ag], Audience::Private));
        assert!(
            html.contains(
                "<td class=\"l\">Google AI</td><td class=\"l\">Gemini</td><td>$10.00</td><td></td>"
            ),
            "{html}"
        );
        assert!(
            html.contains(
                "<td class=\"l\">Google AI</td><td class=\"l\">Gemini</td><td></td><td>$30.00</td>"
            ),
            "{html}"
        );
    }

    #[test]
    fn a_tiny_weighted_price_draws_a_bounded_stack() {
        let t = ToolValue {
            tool: "codex".into(),
            pace: Some(crate::value::Pace {
                days: 28.0,
                monthly_cost_usd: Some(500.0),
                multiple: Some(5000.0),
                price_usd: Some(0.1),
                paid_usd: Some(0.1),
                plans: 1,
            }),
            ..Default::default()
        };
        let svg = super::super::value_svg::value_squares(&page(vec![t.clone()], Audience::Private));
        assert!(svg.matches("<rect").count() <= 60, "{svg}");
        assert!(!svg.contains("=\"-"), "{svg}");
        assert!(svg.contains("5000.0x the price, 60 drawn"), "{svg}");
        // The public page rounds $6.04 to $6; blocks and cap follow 60.4x.
        let mut t = t.clone();
        t.pace = Some(crate::value::Pace {
            monthly_cost_usd: Some(6.04),
            multiple: Some(60.4),
            ..t.pace.unwrap()
        });
        let mut p = page(vec![t], Audience::Private);
        p.round_costs();
        let svg = super::super::value_svg::value_squares(&p);
        assert!(svg.contains("60.4x the price, 60 drawn"), "{svg}");
    }
}
