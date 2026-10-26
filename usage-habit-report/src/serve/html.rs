//! HTML for the dashboard, built from strings. The CSS and JS are embedded so
//! the page is one self-contained document with no outside requests, which
//! is also what the public snapshot (ticket 11) needs.
//!
//! Panels added by later tickets call [`section`] for their frame and only
//! supply a body; see docs/design.md for the order and ids.

use super::data::{
    Heatmap, ModelMatrix, Projects, SessionLine, SessionLists, Spend, StoreFacts, TOOLS, Totals,
    WEEKDAYS, Window,
};
use crate::agents::AgentsReport;
use crate::query::Tokens;
use std::fmt::Write;

pub const CSS: &str = include_str!("../../assets/dashboard.css");
pub const JS: &str = include_str!("../../assets/dashboard.js");

/// Public pages link to generated files and explicitly public repositories.
#[derive(Debug, Clone, Default)]
pub enum Audience {
    #[default]
    Private,
    Public {
        windows: Vec<Window>,
    },
}

impl Audience {
    pub fn is_public(&self) -> bool {
        matches!(self, Self::Public { .. })
    }

    fn usd(&self, cost: Option<f64>, unpriced: i64) -> String {
        if self.is_public() {
            match cost {
                Some(c) => format!("${c:.0}{}", if unpriced > 0 { "*" } else { "" }),
                None => "-".into(),
            }
        } else {
            fmt_usd(cost, unpriced)
        }
    }
}

/// Everything the page shows, already computed; no database here.
#[derive(Debug, Clone)]
pub struct Page {
    pub audience: Audience,
    pub window: Window,
    pub tool: Option<&'static str>,
    /// "Tue 20 Oct 2026, 00:56"
    pub now_label: String,
    /// "UTC-04:00"
    pub tz_label: String,
    /// "28 May 2026"
    pub first_label: Option<String>,
    pub facts: StoreFacts,
    pub heatmap: Heatmap,
    pub totals: Totals,
    pub models: ModelMatrix,
    pub agents: AgentsReport,
    /// Subagent share of tokens per month, over all history.
    pub agent_months: Vec<crate::agents::MonthShare>,
    pub projects: Projects,
    pub sessions: SessionLists,
    pub spend: Spend,
    pub spend_totals: super::data::SpendTotals,
    pub limit_bands: Vec<crate::value::LimitBands>,
    /// Shown in the footer when a rates file names its fetch time.
    pub rates_fetched_at: Option<String>,
    /// How long the data took to load, for the footer.
    pub load_ms: f64,
}

pub fn esc(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            _ => out.push(c),
        }
    }
    out
}

/// 1234567 as "1,234,567".
pub fn fmt_int(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 {
        out.insert(0, '-');
    }
    out
}

/// "$12.34", with a trailing "*" when some requests had no price, or "-".
pub fn fmt_usd(cost: Option<f64>, unpriced: i64) -> String {
    match cost {
        Some(c) if unpriced > 0 => format!("${c:.2}*"),
        Some(c) => format!("${c:.2}"),
        None => "-".into(),
    }
}

/// The frame every panel shares: a section with an id, a title, and a body.
/// `note` is one short sentence under the title (the window, a coverage
/// caveat) or empty.
pub fn section(id: &str, title: &str, note: &str, body: &str) -> String {
    let mut s = String::new();
    let _ = write!(
        s,
        "<section id=\"{}\" aria-labelledby=\"{}-title\">",
        esc(id),
        esc(id)
    );
    let _ = write!(s, "<h2 id=\"{}-title\">{}</h2>", esc(id), esc(title));
    if !note.is_empty() {
        let _ = write!(s, "<p class=\"note\">{note}</p>");
    }
    s.push_str(body);
    s.push_str("</section>\n");
    s
}

fn href(p: &Page, window: Window, tool: Option<&str>) -> String {
    if let Audience::Public { windows } = &p.audience {
        return super::snapshot::filename(window, tool, windows.len() == 1);
    }
    match tool {
        Some(t) => format!("?window={}&amp;tool={}", window.key(), esc(t)),
        None => format!("?window={}", window.key()),
    }
}

fn controls(p: &Page) -> String {
    let mut s = String::from("<nav class=\"controls\" aria-label=\"Window and tool\">");
    s.push_str("<p class=\"group\"><span class=\"group-name\">Window</span>");
    let windows = match &p.audience {
        Audience::Private => &Window::ALL[..],
        Audience::Public { windows } => windows.as_slice(),
    };
    for &w in windows {
        let current = if w == p.window {
            " aria-current=\"page\""
        } else {
            ""
        };
        let _ = write!(
            s,
            " <a href=\"{}\"{current}>{}</a>",
            href(p, w, p.tool),
            w.label()
        );
    }
    s.push_str("</p><p class=\"group\"><span class=\"group-name\">Tool</span>");
    let current = if p.tool.is_none() {
        " aria-current=\"page\""
    } else {
        ""
    };
    let _ = write!(
        s,
        " <a href=\"{}\"{current}>all</a>",
        href(p, p.window, None)
    );
    for t in TOOLS {
        let current = if p.tool == Some(t) {
            " aria-current=\"page\""
        } else {
            ""
        };
        let _ = write!(
            s,
            " <a class=\"tool\" data-tool=\"{t}\" href=\"{}\"{current}>{t}</a>",
            href(p, p.window, Some(t))
        );
    }
    let value_href = if p.audience.is_public() {
        super::snapshot::VALUE_FILENAME
    } else {
        "/value"
    };
    let self_href = href(p, p.window, p.tool);
    let _ = writeln!(
        s,
        "</p><p class=\"group\"><span class=\"group-name\">Page</span> <a href=\"{self_href}\" aria-current=\"page\">usage habits</a> <a href=\"{value_href}\">subscription value</a></p></nav>"
    );
    s
}

fn window_sentence(p: &Page) -> String {
    match p.window {
        Window::All => format!(
            "All time to {}, local time {}.",
            esc(&p.now_label),
            esc(&p.tz_label)
        ),
        w => format!(
            "{} to {}, local time {}.",
            w.label(),
            esc(&p.now_label),
            esc(&p.tz_label)
        ),
    }
}

fn cell_readout(p: &Page, day: usize, hour: usize, c: &super::data::Cell) -> String {
    let span = format!(
        "{} {:02}:00 to {:02}:00",
        WEEKDAYS[day],
        hour,
        (hour + 1) % 24
    );
    if c.requests == 0 {
        return format!("{span}: no requests");
    }
    format!(
        "{span}: {} tokens in {} requests, est. {}",
        fmt_int(c.tokens),
        fmt_int(c.requests),
        p.audience.usd(c.cost_usd, c.unpriced)
    )
}

fn activity(p: &Page) -> String {
    let h = &p.heatmap;
    let ramp = match p.tool {
        Some(t) => format!(" style=\"--ramp: var(--{t})\""),
        None => String::new(),
    };
    let mut body = String::new();
    if h.is_empty() {
        body.push_str("<p class=\"empty\">No requests in this window.");
        if p.facts.requests == 0 {
            body.push_str(" Run <code>usage-habit-report scan</code> to read your logs.");
        } else {
            body.push_str(" Widen the window or clear the tool filter.");
        }
        body.push_str("</p>");
        return body;
    }
    let _ = write!(
        body,
        "<div class=\"activity\"{ramp}><div class=\"map-wrap\"><table id=\"map\" class=\"map\" aria-label=\"Tokens by weekday and hour\"><thead><tr><th scope=\"col\" class=\"corner\"><span class=\"sr\">Weekday</span></th>"
    );
    for hour in 0..24 {
        let _ = write!(
            body,
            "<th scope=\"col\" class=\"h\"{}>{hour}</th>",
            if hour % 3 == 0 {
                " data-major=\"\""
            } else {
                ""
            }
        );
    }
    body.push_str("</tr></thead><tbody>");
    for (d, row) in h.cells.iter().enumerate() {
        let _ = write!(body, "<tr><th scope=\"row\">{}</th>", WEEKDAYS[d]);
        for (hour, c) in row.iter().enumerate() {
            let text = esc(&cell_readout(p, d, hour, c));
            let _ = write!(
                body,
                "<td class=\"c\" data-level=\"{}\" tabindex=\"0\" aria-label=\"{text}\" data-readout=\"{text}\"></td>",
                h.level(c)
            );
        }
        body.push_str("</tr>");
    }
    body.push_str("</tbody></table></div>");
    body.push_str("<div class=\"map-foot\">");
    let idle = "Hover or focus a cell for its figures.";
    let _ = write!(
        body,
        "<p id=\"readout\" class=\"readout\" aria-live=\"polite\" data-idle=\"{idle}\">{idle}</p>"
    );
    body.push_str("<p class=\"legend\" aria-label=\"Shade scale\"><span class=\"legend-name\">Tokens from</span>");
    for (i, t) in h.thresholds.iter().enumerate() {
        let _ = write!(
            body,
            "<span class=\"step\"><i class=\"c\" data-level=\"{}\"></i>{}</span>",
            i + 1,
            fmt_int(*t)
        );
    }
    body.push_str("</p></div></div>");
    body
}

fn tokens_cells(p: &Page, t: &Tokens) -> String {
    format!(
        "<td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>",
        fmt_int(t.requests),
        fmt_int(t.input_tokens),
        fmt_int(t.cache_read_input_tokens),
        fmt_int(t.cache_creation_input_tokens),
        fmt_int(t.output_tokens),
        p.audience.usd(t.estimated_cost_usd, t.unpriced_requests)
    )
}

fn totals(p: &Page) -> String {
    let t = &p.totals;
    if t.total.requests == 0 {
        return String::from("<p class=\"empty\">Nothing to add up in this window.</p>");
    }
    let mut body = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\">Requests</th><th scope=\"col\">Uncached input</th><th scope=\"col\">Cache read</th><th scope=\"col\">Cache write</th><th scope=\"col\">Output</th><th scope=\"col\">Est. cost</th></tr></thead><tbody>",
    );
    for row in &t.by_tool {
        let _ = write!(
            body,
            "<tr><th scope=\"row\"><a class=\"tool\" data-tool=\"{0}\" href=\"{1}\">{0}</a></th>{2}</tr>",
            esc(&row.tool),
            href(p, p.window, Some(&row.tool)),
            tokens_cells(p, &row.tokens)
        );
    }
    let _ = write!(
        body,
        "<tr class=\"total\"><th scope=\"row\">Total</th>{}</tr>",
        tokens_cells(p, &t.total)
    );
    body.push_str("</tbody></table></div>");
    if t.total.unpriced_requests > 0 {
        let _ = write!(
            body,
            "<p class=\"note\">* leaves out {} requests whose model has no price.</p>",
            fmt_int(t.total.unpriced_requests)
        );
    }
    body
}

/// "4 weeks to Tue 20 Oct 2026, 00:56" or "All time to ...".
fn window_phrase(p: &Page) -> String {
    match p.window {
        Window::All => format!("All time to {}", esc(&p.now_label)),
        w => format!("{} to {}", w.label(), esc(&p.now_label)),
    }
}

/// " --since 4w" or "" for all time.
fn since_flag(p: &Page) -> String {
    p.window
        .since_arg()
        .map(|a| format!(" --since {a}"))
        .unwrap_or_default()
}

fn tool_name(t: &str) -> String {
    format!("<span class=\"tool\" data-tool=\"{0}\">{0}</span>", esc(t))
}

/// "a", "a and b", "a, b and c".
fn and_list(items: &[String]) -> String {
    match items {
        [] => String::new(),
        [one] => one.clone(),
        [rest @ .., last] => format!("{} and {last}", rest.join(", ")),
    }
}

/// The window and scope every secondary panel's note opens with, plus a
/// sentence naming the tools that have no requests in the window.
fn scope(p: &Page) -> String {
    let mut s = window_phrase(p);
    if let Some(t) = p.tool {
        let _ = write!(s, ", {} only", tool_name(t));
    }
    s.push('.');
    if p.tool.is_none() && p.totals.total.requests > 0 {
        let missing: Vec<String> = TOOLS
            .iter()
            .filter(|t| !p.totals.by_tool.iter().any(|r| r.tool == **t))
            .map(|t| tool_name(t))
            .collect();
        if !missing.is_empty() {
            let _ = write!(
                s,
                " No {} requests in this window, so {} not covered.",
                and_list(&missing),
                if missing.len() == 1 {
                    "it is"
                } else {
                    "they are"
                }
            );
        }
    }
    s
}

/// Empty state for a panel with nothing in the window.
fn nothing_here(p: &Page) -> String {
    if p.facts.requests == 0 {
        "<p class=\"empty\">No usage recorded yet. Run <code>usage-habit-report scan</code> to read your logs.</p>".into()
    } else {
        "<p class=\"empty\">No requests in this window. Widen the window or clear the tool filter.</p>".into()
    }
}

/// "45s", "12m 05s", "3h 07m": the same form `query session` prints.
pub fn fmt_elapsed(secs: i64) -> String {
    let (h, m, s) = (secs / 3600, secs / 60 % 60, secs % 60);
    if h > 0 {
        format!("{h}h {m:02}m")
    } else if m > 0 {
        format!("{m}m {s:02}s")
    } else {
        format!("{s}s")
    }
}

fn fmt_pct(x: Option<f64>) -> String {
    x.map_or("-".into(), |v| format!("{:.1} %", v * 100.0))
}

/// A stepped bar: `on` filled units of `of`, unfilled ones drawn as insets
/// when `track` is set. `tool` gives the bar that tool's hue.
pub(crate) fn units(on: usize, of: usize, track: bool, tool: Option<&str>) -> String {
    let mut s = String::from("<span class=\"units\"");
    if let Some(t) = tool {
        let _ = write!(s, " data-tool=\"{}\"", esc(t));
    }
    s.push_str(" aria-hidden=\"true\">");
    for i in 0..of {
        if i < on {
            s.push_str("<i></i>");
        } else if track {
            s.push_str("<i class=\"off\"></i>");
        }
    }
    s.push_str("</span>");
    s
}

/// Units out of 20 for a share, rounded, at least one for any non-zero share.
fn share_units(x: f64) -> usize {
    let n = (x * 20.0).round() as usize;
    if x > 0.0 { n.clamp(1, 20) } else { 0 }
}

fn models_panel(p: &Page) -> String {
    let m = &p.models;
    if m.total == 0 {
        return nothing_here(p);
    }
    if m.labelled() == 0 {
        return "<p class=\"empty\">No labels yet. Run <code>usage-habit-report rebuild</code> to classify existing sessions.</p>".into();
    }
    let mut body = super::radar::render(m);
    body.push_str("<details class=\"model-counts\"><summary>Exact request counts by task kind</summary><div class=\"table-wrap\"><table class=\"ledger matrix\"><thead><tr><th scope=\"col\">Model</th>");
    for k in &m.kinds {
        let _ = write!(body, "<th scope=\"col\">{}</th>", esc(k));
    }
    body.push_str("<th scope=\"col\">Total</th></tr></thead><tbody>");
    for r in &m.rows {
        let name = match (&r.model, r.models) {
            (_, n) if n > 1 => format!("{n} other models"),
            (Some(model), _) => esc(model),
            (None, _) => "<span class=\"untitled\">unknown model</span>".into(),
        };
        let _ = write!(body, "<tr><th scope=\"row\">{name}</th>");
        for (k, n) in m.kinds.iter().zip(&r.counts) {
            let figure = if *n == 0 {
                "<span class=\"zero\">0</span>".to_string()
            } else {
                fmt_int(*n)
            };
            let _ = write!(
                body,
                "<td title=\"{} requests, {}\"><i class=\"c\" data-level=\"{}\" aria-hidden=\"true\"></i>{figure}</td>",
                fmt_int(*n),
                esc(k),
                m.level(*n)
            );
        }
        let _ = write!(body, "<td>{}</td></tr>", fmt_int(r.total));
    }
    body.push_str("<tr class=\"total\"><th scope=\"row\">Total</th>");
    for n in &m.col_totals {
        let _ = write!(body, "<td>{}</td>", fmt_int(*n));
    }
    let _ = write!(
        body,
        "<td>{}</td></tr></tbody></table></div>",
        fmt_int(m.total)
    );
    body.push_str("<p class=\"legend\" aria-label=\"Shade scale\"><span class=\"legend-name\">Requests from</span>");
    for (i, t) in m.thresholds.iter().enumerate() {
        let _ = write!(
            body,
            "<span class=\"step\"><i class=\"c\" data-level=\"{}\"></i>{}</span>",
            i + 1,
            fmt_int(*t)
        );
    }
    body.push_str("</p></details>");
    body
}

/// Whole percent of a share, "<1 %" for a non-zero share that rounds to 0.
fn whole_pct(x: Option<f64>) -> String {
    match x {
        None => "-".into(),
        Some(v) if v > 0.0 && v < 0.005 => "&lt;1 %".into(),
        Some(v) => format!("{:.0} %", v * 100.0),
    }
}

/// How much of the work went to subagent threads: one row of 20 squares per
/// tool for the window, or per month over all history, behind the radio
/// toggle. Squares are the subagent share of tokens.
fn agents_panel(p: &Page) -> String {
    let a = &p.agents;
    let row = |label: String, share: Option<f64>, tool: Option<&str>, sessions: Option<String>| {
        let on = share.map_or(0, share_units);
        format!(
            "<tr><th scope=\"row\">{label}</th><td>{}</td><td class=\"n\">{}</td>{}</tr>",
            units(on, 20, true, tool),
            whole_pct(share),
            sessions.map_or(String::new(), |s| format!("<td class=\"s\">{s}</td>"))
        )
    };
    let of = |part: usize, whole: usize| {
        format!("{} of {}", fmt_int(part as i64), fmt_int(whole as i64))
    };

    // By tool, in the page's tool order, then all tools together.
    let tools: Vec<_> = super::data::TOOLS
        .iter()
        .filter_map(|t| a.by_tool.iter().find(|b| b.tool == *t))
        .filter(|b| b.top_level_sessions > 0)
        .collect();
    let mut by_tool = String::new();
    for b in &tools {
        by_tool.push_str(&row(
            tool_name(&b.tool),
            b.subagent_token_share,
            Some(&b.tool),
            Some(of(b.sessions_with_subagents, b.top_level_sessions)),
        ));
    }
    if tools.len() > 1 {
        let (sub, total) = tools.iter().fold((0, 0), |(s, t), b| {
            (s + b.subagent_tokens, t + b.total_tokens)
        });
        by_tool.push_str(&row(
            "All tools".into(),
            (total > 0).then(|| sub as f64 / total as f64),
            None,
            Some(of(
                a.totals.sessions_with_subagents,
                a.totals.top_level_sessions,
            )),
        ));
    }

    // By month: the last twelve, so no month name repeats.
    let months = &p.agent_months[p.agent_months.len().saturating_sub(12)..];
    let mut by_month = String::new();
    for m in months {
        let mut label = super::value_svg::month_abbr(&m.month);
        if m.partial {
            label.push_str(", so far");
        }
        by_month.push_str(&row(
            label,
            (m.total_tokens > 0).then(|| m.subagent_tokens as f64 / m.total_tokens as f64),
            None,
            None,
        ));
    }

    // Captions, headers and the legend render with no data too, so their
    // words reach the privacy guard's template vocabulary.
    let empty = nothing_here(p);
    let table = |head: &str, rows: &str| {
        format!(
            "<div class=\"panel\"><table><thead><tr><th scope=\"col\"><span class=\"sr\">Row</span></th><th scope=\"col\"><span class=\"sr\">Squares</span></th><th scope=\"col\" class=\"n\">tokens</th>{head}</tr></thead><tbody>{rows}</tbody></table>{}</div>",
            if rows.is_empty() { empty.as_str() } else { "" }
        )
    };
    let mut body = format!(
        "<figure class=\"chart-fig toggled handoff\"><input type=\"radio\" class=\"sr\" name=\"handoff\" id=\"handoff-tool\" checked><input type=\"radio\" class=\"sr\" name=\"handoff\" id=\"handoff-month\"><figcaption><div class=\"toggle\"><label for=\"handoff-tool\">By tool</label><label for=\"handoff-month\">By month</label></div><p>Share of tokens spent in subagent threads vs main threads. Each square is 5 %. By month covers all history.</p></figcaption>{}{}<p class=\"legend\"><span><i class=\"on\"></i>subagent threads</span><span><i class=\"off\"></i>main threads</span></p></figure>",
        table(
            "<th scope=\"col\" class=\"s\">sessions with subagents</th>",
            &by_tool
        ),
        table("", &by_month),
    );

    if p.audience.is_public() {
        // Per-session fan-outs and per-tool coverage notes stay private.
        return body;
    } else if a.heaviest_fanouts.is_empty() {
        body.push_str("<p class=\"empty\">No session in this window spawned subagents.</p>");
    } else {
        body.push_str("<div class=\"table-wrap\"><table class=\"ledger\"><caption>Heaviest fan-outs, by most subagents running at once</caption><thead><tr><th scope=\"col\">Session</th><th scope=\"col\" class=\"l\">Tool</th><th scope=\"col\">Subagents</th><th scope=\"col\">Max concurrent</th><th scope=\"col\">Subagent tokens</th><th scope=\"col\">Share of session</th></tr></thead><tbody>");
        for f in &a.heaviest_fanouts {
            let _ = write!(
                body,
                "<tr><td class=\"title\">{}</td><td class=\"l\">{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                title_cell(f.display_title.as_deref().or(f.title.as_deref())),
                tool_name(&f.tool),
                fmt_int(f.subagent_count as i64),
                fmt_int(f.max_concurrent_fanout as i64),
                fmt_int(f.subagent_tokens),
                fmt_pct(f.subagent_token_share)
            );
        }
        body.push_str("</tbody></table></div>");
    }

    // Coverage, as sentences, for the tools in scope.
    body.push_str("<div class=\"coverage\">");
    for (tool, c) in &a.coverage {
        if p.tool.is_some_and(|t| t != tool) {
            continue;
        }
        let _ = write!(
            body,
            "<p>{}{} {}</p>",
            tool_name(tool),
            if c.supported { ":" } else { " is not covered:" },
            esc(&c.note)
        );
    }
    for bt in &a.by_tool {
        if bt.parents_with_imported_children > 0 {
            body.push_str(&format!("<p>{} {} parents gained children through T3 delegation records, including {} timing-matched links.</p>",
                bt.parents_with_imported_children, tool_name(&bt.tool), bt.timing_matched_links));
        }
        if bt.unlinked_subagent_sessions > 0 {
            let _ = write!(
                body,
                "<p>{} {} subagent sessions have no linked parent in this window; their active time counts as separate top-level sessions. They count in the monthly token share but not in the per-tool rows or the fan-outs.</p>",
                fmt_int(bt.unlinked_subagent_sessions as i64),
                tool_name(&bt.tool)
            );
        }
    }
    body.push_str("</div>");
    body
}

fn title_cell(title: Option<&str>) -> String {
    match title {
        Some(t) if !t.trim().is_empty() => esc(t),
        _ => "<span class=\"untitled\">untitled session</span>".into(),
    }
}

/// Ten units split by each tool's share of `total`, largest remainder first.
fn tool_mix(tools: &[(String, i64, Option<f64>)]) -> String {
    let total: i64 = tools.iter().map(|t| t.1).sum();
    if total == 0 {
        return String::new();
    }
    let mut alloc: Vec<(usize, f64)> = tools
        .iter()
        .map(|t| {
            let exact = t.1 as f64 * 10.0 / total as f64;
            ((exact.floor() as usize).max(1), exact)
        })
        .collect();
    let mut used: usize = alloc.iter().map(|a| a.0).sum();
    while used < 10 {
        let (i, _) = alloc
            .iter()
            .enumerate()
            .max_by(|a, b| (a.1.1 - a.1.0 as f64).total_cmp(&(b.1.1 - b.1.0 as f64)))
            .unwrap_or((0, &(0, 0.0)));
        alloc[i].0 += 1;
        used += 1;
    }
    let label = tools
        .iter()
        .map(|t| format!("{} {:.0} %", t.0, t.1 as f64 * 100.0 / total as f64))
        .collect::<Vec<_>>()
        .join(", ");
    let mut s = format!(
        "<span class=\"units\" role=\"img\" aria-label=\"{0}\" title=\"Requests: {0}\">",
        esc(&label)
    );
    for (t, (n, _)) in tools.iter().zip(&alloc) {
        for _ in 0..*n {
            let _ = write!(s, "<i data-tool=\"{}\"></i>", esc(&t.0));
        }
    }
    s.push_str("</span> <span class=\"mix\">");
    for (i, t) in tools.iter().enumerate() {
        let _ = write!(
            s,
            "{}<b data-tool=\"{}\">{:.0}</b>",
            if i == 0 { "" } else { " / " },
            esc(&t.0),
            t.1 as f64 * 100.0 / total as f64
        );
    }
    s.push_str(" %</span>");
    s
}

fn all_tokens(t: &Tokens) -> i64 {
    t.input_tokens + t.cache_read_input_tokens + t.cache_creation_input_tokens + t.output_tokens
}

fn public_project(name: &str, url: Option<&str>) -> String {
    match url {
        Some(url) => format!(
            "<a href=\"{}\" rel=\"noopener\">{}</a>",
            esc(url),
            esc(name)
        ),
        None => esc(name),
    }
}

fn projects_panel(p: &Page) -> String {
    let pr = &p.projects;
    if pr.lines.is_empty() {
        return nothing_here(p);
    }
    let public = p.audience.is_public();
    let time_header = if public {
        ""
    } else {
        "<th scope=\"col\">Active time</th>"
    };
    let mut body = format!(
        "<div class=\"table-wrap\"><table class=\"ledger\"><thead><tr><th scope=\"col\">Project</th><th scope=\"col\">Sessions</th>{time_header}<th scope=\"col\">Tokens</th><th scope=\"col\">Est. cost</th><th scope=\"col\" class=\"l\">Tools</th></tr></thead><tbody>",
    );
    for l in &pr.lines {
        let r = &l.row;
        let name = match (&r.project, &r.name) {
            (_, Some(name)) if p.audience.is_public() => {
                public_project(name, l.project_url.as_deref())
            }
            (Some(path), Some(name)) => {
                format!("<span title=\"{}\">{}</span>", esc(path), esc(name))
            }
            _ => "<span class=\"untitled\">no known directory</span>".into(),
        };
        let _ = write!(
            body,
            "<tr><th scope=\"row\">{name}</th><td>{}</td>{}<td>{}</td><td>{}</td><td class=\"bar\">{}</td></tr>",
            fmt_int(r.sessions),
            if public {
                String::new()
            } else {
                format!("<td>{}</td>", fmt_elapsed(l.active_seconds))
            },
            fmt_int(all_tokens(&r.tokens)),
            p.audience
                .usd(r.tokens.estimated_cost_usd, r.tokens.unpriced_requests),
            tool_mix(&l.tools)
        );
    }
    body.push_str("</tbody></table></div>");
    let mut foot = String::new();
    if pr.count > pr.lines.len() {
        let _ = write!(
            foot,
            "The {} costliest of {} projects with activity in this window. ",
            pr.lines.len(),
            fmt_int(pr.count as i64)
        );
    }
    foot.push_str(if p.audience.is_public() {
        "Linked projects are public on GitHub. Other project names are pseudonyms shared across this export. Tool shares are percentages of requests."
    } else {
        "Hover a name for its full path. Tool shares are percentages of requests."
    });
    if pr.lines.iter().any(|l| l.row.tokens.unpriced_requests > 0) {
        foot.push_str(" * leaves out requests whose model has no price.");
    }
    let _ = write!(body, "<p class=\"note\">{foot}</p>");
    body
}

fn session_table(p: &Page, caption: &str, rows: &[SessionLine]) -> String {
    let title_header = if p.audience.is_public() {
        ""
    } else {
        "<th scope=\"col\">Session</th>"
    };
    let mut s = format!(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>{caption}</caption><thead><tr>{title_header}<th scope=\"col\" class=\"l\">Tool</th><th scope=\"col\" class=\"l\">Project</th><th scope=\"col\">Active time</th><th scope=\"col\">Requests</th><th scope=\"col\">Est. cost</th></tr></thead><tbody>"
    );
    for r in rows {
        let project = match (&r.project, &r.project_name) {
            (_, Some(name)) if p.audience.is_public() => {
                public_project(name, r.project_url.as_deref())
            }
            (Some(path), Some(name)) => {
                format!("<span title=\"{}\">{}</span>", esc(path), esc(name))
            }
            (Some(path), None) => esc(&crate::project::project_name(path)),
            _ => "<span class=\"untitled\">none</span>".into(),
        };
        let title = if p.audience.is_public() {
            String::new()
        } else {
            format!(
                "<td class=\"title\">{}</td>",
                title_cell(r.title.as_deref())
            )
        };
        let _ = write!(
            s,
            "<tr>{title}<td class=\"l\">{}</td><td class=\"l\">{project}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            tool_name(&r.tool),
            fmt_elapsed(r.active_seconds),
            fmt_int(r.requests),
            p.audience.usd(r.cost_usd, r.unpriced)
        );
    }
    s.push_str("</tbody></table></div>");
    s
}

fn sessions_panel(p: &Page) -> String {
    let sl = &p.sessions;
    if sl.longest.is_empty() {
        return nothing_here(p);
    }
    if p.audience.is_public() {
        return format!(
            "<p>{} top-level sessions across {} projects had activity in this window; {} of them spawned subagents. Per-session figures are left out of this export.</p>",
            fmt_int(p.agents.totals.top_level_sessions as i64),
            fmt_int(p.projects.count as i64),
            fmt_int(p.agents.totals.sessions_with_subagents as i64)
        );
    }
    let mut body = String::from("<div class=\"stack\">");
    body.push_str(&session_table(p, "Longest, by active time", &sl.longest));
    if sl.costliest.is_empty() {
        body.push_str("<p class=\"empty\">No priced requests in this window. Run <code>usage-habit-report rates update</code>, then any <code>query</code>, to price them.</p>");
    } else {
        body.push_str(&session_table(
            p,
            "Costliest, by estimated cost",
            &sl.costliest,
        ));
    }
    body.push_str("</div>");
    body
}

fn cache_panel(p: &Page) -> String {
    let t = &p.totals;
    if t.total.requests == 0 {
        return nothing_here(p);
    }
    let mut body = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><thead><tr><th scope=\"col\">Tool</th><th scope=\"col\">Cache read</th><th scope=\"col\">All input</th><th scope=\"col\">Share</th><th scope=\"col\" class=\"l\"><span class=\"sr\">Bar</span></th></tr></thead><tbody>",
    );
    let row = |name: String, k: &Tokens, tool: Option<&str>, class: &str| {
        let ratio = super::data::cache_read_ratio(k);
        format!(
            "<tr{class}><th scope=\"row\">{name}</th><td>{}</td><td>{}</td><td>{}</td><td class=\"bar\">{}</td></tr>",
            fmt_int(k.cache_read_input_tokens),
            fmt_int(k.input_tokens + k.cache_read_input_tokens + k.cache_creation_input_tokens),
            fmt_pct(ratio),
            units(share_units(ratio.unwrap_or(0.0)), 20, true, tool)
        )
    };
    for r in &t.by_tool {
        body.push_str(&row(tool_name(&r.tool), &r.tokens, Some(&r.tool), ""));
    }
    if t.by_tool.len() > 1 {
        body.push_str(&row("All tools".into(), &t.total, None, " class=\"total\""));
    }
    body.push_str("</tbody></table></div>");
    let no_writes: Vec<String> = t
        .by_tool
        .iter()
        .filter(|r| r.tokens.cache_creation_input_tokens == 0)
        .map(|r| tool_name(&r.tool))
        .collect();
    if !no_writes.is_empty() {
        let _ = write!(
            body,
            "<p class=\"note\">No cache writes recorded for {} in this window, so {} share is cache reads over uncached input plus cache reads.</p>",
            and_list(&no_writes),
            if no_writes.len() == 1 { "its" } else { "their" }
        );
    }
    body
}

/// Height of the tallest month column, its width, and the space between.
const STACK_PLOT: f64 = 160.0;
/// Width the month slots share; the columns fill it whatever their count.
const STACK_WIDTH: f64 = 720.0;
/// Months shown; older ones stay in the table below.
const STACK_MONTHS: usize = 6;

/// Spend by month as stacked columns on one dollar scale, one segment per
/// tool. The current month adds a dashed outline up to its projection: each
/// tool's cost so far over its own days of history in the month, scaled to
/// the whole month.
fn spend_chart(p: &Page) -> String {
    let sp = &p.spend;
    if !sp.by_month {
        return String::new();
    }
    let last = sp.periods.len().saturating_sub(1);
    let first = sp.periods.len().saturating_sub(STACK_MONTHS);
    // (label, per-tool costs, projected total for the current month)
    let months: Vec<_> = sp
        .periods
        .iter()
        .enumerate()
        .skip(first)
        .map(|(i, per)| {
            let costs: Vec<f64> = per.cells.iter().map(|c| c.0.unwrap_or(0.0)).collect();
            let projected = sp.current.as_ref().filter(|_| i == last).map(|cur| {
                costs
                    .iter()
                    .zip(&cur.covered)
                    // Under a day of history says little about a month.
                    .map(|(c, d)| {
                        if *d >= 1.0 {
                            c * cur.days / d.min(cur.days)
                        } else {
                            *c
                        }
                    })
                    .sum::<f64>()
            });
            (per, costs, projected)
        })
        .collect();
    let peak = months
        .iter()
        .map(|(_, c, proj)| c.iter().sum::<f64>().max(proj.unwrap_or(0.0)))
        .fold(0.0, f64::max);
    if peak <= 0.0 {
        return String::new();
    }
    let h = |usd: f64| STACK_PLOT * usd.max(0.0) / peak;
    let top = 18.0;
    let base = top + STACK_PLOT;
    let slot = STACK_WIDTH / months.len() as f64;
    let col = (slot * 0.6).min(120.0);
    let height = base + 40.0;
    // Whole dollars for both audiences; the table below keeps the detail.
    let usd = |v: f64| format!("${}", fmt_int(v.round() as i64));
    let mut desc = Vec::new();
    let mut body = String::new();
    for (i, (per, costs, projected)) in months.iter().enumerate() {
        let mid = (i as f64 + 0.5) * slot;
        let x = mid - col / 2.0;
        let total: f64 = costs.iter().sum();
        let parts = sp
            .tools
            .iter()
            .zip(costs)
            .filter(|(_, c)| **c > 0.0)
            .map(|(t, c)| format!("{t} {}", usd(*c)))
            .collect::<Vec<_>>()
            .join(", ");
        let mut text = format!("{}: est. {} ({parts})", per.label, usd(total));
        if let Some(v) = projected {
            text.push_str(&format!(", on pace for {}", usd(*v)));
        }
        let _ = write!(body, "<g><title>{}</title>", esc(&text));
        desc.push(text);
        if let Some(v) = projected.filter(|v| *v > total) {
            let _ = write!(
                body,
                "<rect class=\"st-pace\" x=\"{x:.1}\" y=\"{:.1}\" width=\"{col:.1}\" height=\"{:.1}\"/>",
                base - h(v),
                h(v) - h(total)
            );
        }
        let mut y = base;
        for (t, c) in sp.tools.iter().zip(costs) {
            if *c <= 0.0 {
                continue;
            }
            y -= h(*c);
            let _ = write!(
                body,
                "<rect class=\"st-seg\" data-tool=\"{}\" x=\"{x:.1}\" y=\"{y:.1}\" width=\"{col:.1}\" height=\"{:.1}\"/>",
                esc(t),
                h(*c)
            );
        }
        let (label, at) = match projected {
            Some(v) => (format!("{} pace", usd(*v)), v.max(total)),
            None => (usd(total), total),
        };
        let month = per.label.split(' ').next().unwrap_or(&per.label);
        let _ = write!(
            body,
            "<text class=\"st-value\" x=\"{mid:.1}\" y=\"{:.1}\">{}</text><text class=\"st-month\" x=\"{mid:.1}\" y=\"{:.1}\">{}</text>",
            base - h(at) - 5.0,
            esc(&label),
            base + 16.0,
            esc(month),
        );
        if projected.is_some_and(|v| h(v) - h(total) > 20.0) {
            let _ = write!(
                body,
                "<text class=\"st-value\" x=\"{mid:.1}\" y=\"{:.1}\">{}</text>",
                base - h(total) - 5.0,
                usd(total)
            );
        }
        if let Some(cur) = sp.current.as_ref().filter(|_| projected.is_some()) {
            let so_far = cur.covered.iter().copied().fold(0.0, f64::max);
            let _ = write!(
                body,
                "<text class=\"st-note\" x=\"{mid:.1}\" y=\"{:.1}\">{:.0} of {:.0} days</text>",
                base + 32.0,
                so_far,
                cur.days
            );
        }
        body.push_str("</g>");
    }
    let _ = write!(
        body,
        "<line class=\"st-base\" x1=\"-4\" x2=\"{:.1}\" y1=\"{base:.1}\" y2=\"{base:.1}\"/>",
        STACK_WIDTH + 4.0
    );
    let key = if sp.tools.len() > 1 {
        format!(
            "<p class=\"key\">{}</p>",
            sp.tools
                .iter()
                .map(|t| tool_name(t))
                .collect::<Vec<_>>()
                .join(" ")
        )
    } else {
        String::new()
    };
    let width = STACK_WIDTH + 8.0;
    format!(
        "<figure class=\"chart-fig spend-stack\"><figcaption><p>Each column is a month's use at API list prices, split by tool.{}</p>{key}</figcaption><svg viewBox=\"-4 0 {width:.0} {height:.0}\" role=\"img\" aria-labelledby=\"spend-title spend-desc\"><title id=\"spend-title\">Est. cost by month and tool</title><desc id=\"spend-desc\">{}</desc>{body}</svg></figure>",
        if sp.current.is_some() {
            " The dashed outline is where this month ends at each tool's pace so far."
        } else {
            ""
        },
        esc(&desc.join("; "))
    )
}

/// Fewest closed windows a tool needs for a bar.
const BANDS_MIN_WINDOWS: i64 = 5;
const BANDS_LABEL: f64 = 120.0;
const BANDS_PLOT: f64 = 400.0;
const BANDS_ROW: f64 = 34.0;
const BAND_NAMES: [&str; 4] = ["under 25 %", "25 to 75 %", "75 to 99 %", "ran out"];

/// Every closed limit window as one bar per tool, split by how full the
/// window got before it reset. One panel per window length, with the
/// value page's radio toggle between them.
fn limits_panel(p: &Page) -> String {
    let mut panels: Vec<(&str, i64, String)> = Vec::new();
    for window in ["session", "weekly"] {
        let rows: Vec<_> = p
            .limit_bands
            .iter()
            .filter(|b| b.window == window && b.windows >= BANDS_MIN_WINDOWS)
            .collect();
        let Some(first) = rows.first() else {
            continue;
        };
        let mut svg = String::new();
        let mut desc = Vec::new();
        for (i, b) in rows.iter().enumerate() {
            let y = 8.0 + i as f64 * BANDS_ROW;
            let out = 100.0 * b.bands[3] as f64 / b.windows as f64;
            let _ = write!(
                svg,
                "<g data-tool=\"{0}\"><text class=\"lb-tool\" x=\"0\" y=\"{1:.1}\">{0}</text>",
                esc(&b.tool),
                y + 15.0
            );
            let mut x = BANDS_LABEL;
            for (j, n) in b.bands.iter().enumerate() {
                let w = BANDS_PLOT * *n as f64 / b.windows as f64;
                if w > 0.0 {
                    let _ = write!(
                        svg,
                        "<rect class=\"lb-band lb-{j}\" x=\"{x:.1}\" y=\"{y:.1}\" width=\"{w:.1}\" height=\"20\"><title>{}: {n} of {} windows {}</title></rect>",
                        esc(&b.tool),
                        b.windows,
                        BAND_NAMES[j]
                    );
                }
                x += w;
            }
            let _ = write!(
                svg,
                "<text class=\"lb-value\" x=\"{:.1}\" y=\"{:.1}\"><tspan>{out:.0} %</tspan> ran out</text><text class=\"lb-note\" x=\"720\" y=\"{:.1}\">{} windows</text></g>",
                BANDS_LABEL + BANDS_PLOT + 16.0,
                y + 15.0,
                y + 15.0,
                fmt_int(b.windows)
            );
            desc.push(format!(
                "{}: {} of {} windows ran out; {}",
                b.tool,
                b.bands[3],
                b.windows,
                (0..3)
                    .map(|j| format!("{} {}", b.bands[j], BAND_NAMES[j]))
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        let height = 8.0 + rows.len() as f64 * BANDS_ROW;
        panels.push((
            window,
            first.window_hours,
            format!(
                "<svg viewBox=\"0 0 720 {height:.0}\" role=\"img\" aria-label=\"{}\">{svg}</svg>",
                esc(&desc.join("; "))
            ),
        ));
    }
    // The caption and legend render even with no bars, so their words
    // reach the privacy guard's template vocabulary.
    let caption = format!(
        "<p>Each bar is every closed window, by how full it got before it reset. A tool needs {BANDS_MIN_WINDOWS} closed windows for a bar.</p>"
    );
    let legend: String = BAND_NAMES
        .iter()
        .enumerate()
        .map(|(j, n)| format!("<span><i class=\"lb-{j}\"></i>{n}</span>"))
        .collect();
    let legend = format!("<div class=\"legend lb-legend\">{legend}</div>");
    if panels.is_empty() {
        return format!(
            "<figure class=\"chart-fig limits\"><figcaption>{caption}</figcaption><p class=\"empty\">No tool has {BANDS_MIN_WINDOWS} closed limit windows in this window yet, so none ran out.</p>{legend}</figure>"
        );
    }
    let name = |w: &str, hours: i64| {
        if w == "weekly" {
            "Weekly".to_owned()
        } else {
            format!("{hours} hr")
        }
    };
    let mut inputs = String::new();
    let mut labels = String::new();
    let mut bodies = String::new();
    for (i, (w, hours, body)) in panels.iter().enumerate() {
        if panels.len() > 1 {
            let _ = write!(
                inputs,
                "<input type=\"radio\" class=\"sr\" name=\"limits\" id=\"limits-{w}\"{}>",
                if i == 0 { " checked" } else { "" }
            );
            let _ = write!(
                labels,
                "<label for=\"limits-{w}\">{}</label>",
                name(w, *hours)
            );
        }
        let _ = write!(bodies, "<div class=\"panel\">{body}</div>");
    }
    let toggle = match panels.as_slice() {
        [(w, hours, _)] => format!("<p class=\"lb-only\">{}</p>", name(w, *hours)),
        _ => format!("<div class=\"toggle\">{labels}</div>"),
    };
    format!(
        "<figure class=\"chart-fig toggled limits\">{inputs}<figcaption>{toggle}{caption}</figcaption>{bodies}{legend}</figure>"
    )
}

fn spend_panel(p: &Page) -> String {
    let sp = &p.spend;
    if p.totals.total.requests == 0 || sp.periods.is_empty() {
        return nothing_here(p);
    }
    let period = if sp.by_month { "Month" } else { "Week" };
    let mut body = spend_chart(p);
    let _ = write!(
        body,
        "<div class=\"table-wrap\"><table class=\"ledger quiet\"><thead><tr><th scope=\"col\">{period}</th>"
    );
    for t in &sp.tools {
        let _ = write!(body, "<th scope=\"col\">{}</th>", tool_name(t));
    }
    let multi = sp.tools.len() > 1;
    if multi {
        body.push_str("<th scope=\"col\">Total</th>");
    }
    body.push_str("</tr></thead><tbody>");
    for (per, total) in sp.periods.iter().zip(&p.spend_totals.rows) {
        let _ = write!(body, "<tr><th scope=\"row\">{}</th>", esc(&per.label));
        for (cost, unpriced) in &per.cells {
            let _ = write!(body, "<td>{}</td>", p.audience.usd(*cost, *unpriced));
        }
        if multi {
            let _ = write!(body, "<td>{}</td>", p.audience.usd(total.0, total.1));
        }
        body.push_str("</tr>");
    }
    body.push_str("<tr class=\"total\"><th scope=\"row\">Total</th>");
    for (cost, unpriced) in &p.spend_totals.columns {
        let _ = write!(body, "<td>{}</td>", p.audience.usd(*cost, *unpriced));
    }
    let all = p.spend_totals.all;
    if multi {
        let _ = write!(body, "<td>{}</td>", p.audience.usd(all.0, all.1));
    }
    body.push_str("</tr></tbody></table></div>");
    if all.1 > 0 {
        let _ = write!(
            body,
            "<p class=\"note\">* leaves out {} requests whose model has no price.</p>",
            fmt_int(all.1)
        );
    }
    body
}

/// The ticket 10 panels, in the order docs/design.md gives.
fn panels(p: &Page) -> String {
    let since = since_flag(p);
    let scope = scope(p);
    let tool_rows = |what: &str| match p.tool {
        Some(t) => format!(" ({t} {what})"),
        None => String::new(),
    };
    let mut s = String::new();
    s.push_str(&section(
        "models",
        "Model by task kind",
        &format!(
            "{scope} Requests by canonical model and the session's task label; a subagent counts under its parent's label. Column totals match the label totals of <code>query summary --group-by label{since}</code>{}.",
            tool_rows("rows")
        ),
        &models_panel(p),
    ));
    s.push_str(&section(
        "agents",
        "How much of my work I hand to subagents",
        &format!(
            "{scope} Tokens are uncached input plus output. Figures match <code>query agents{since}</code>{}.",
            tool_rows("row of by_tool")
        ),
        &agents_panel(p),
    ));
    let idle_minutes = crate::store::IDLE_GAP_SECONDS / 60;
    s.push_str(&section(
        "projects",
        if p.audience.is_public() { "Cost by project" } else { "Time and cost by project" },
        &format!(
            "{scope} Sessions, tokens and cost match <code>query projects{since}</code>{}.{}",
            match p.tool {
                Some(t) => format!(" counting {t} only"),
                None => String::new(),
            },
            if p.audience.is_public() { String::new() } else { format!(" Active time adds up top-level sessions after removing gaps longer than {idle_minutes} minutes. Linked child activity bridges their parent's gaps; unlinked children count separately.") }
        ),
        &projects_panel(p),
    ));
    s.push_str(&section(
        "sessions",
        if p.audience.is_public() { "Sessions" } else { "Longest and costliest sessions" },
        &if p.audience.is_public() {
            format!("{scope} Counts match <code>query agents{since}</code> and <code>query projects{since}</code>.")
        } else {
            format!(
                "{scope} Top-level sessions with a request in the window. Active time covers the whole session and its linked children; requests and cost count its own requests in the window, as <code>query session &lt;id&gt;{since}</code> does. Titles are the first line of the opening prompt."
            )
        },
        &sessions_panel(p),
    ));
    s.push_str(&section(
        "cache",
        "Cache-read ratio",
        &format!(
            "{scope} Cache-read tokens as a share of all input: uncached, cache read and cache write. Same figures as the estimated API cost section."
        ),
        &cache_panel(p),
    ));
    s.push_str(&section(
        "spend",
        if p.spend.by_month {
            "Spend by month"
        } else {
            "Spend by week"
        },
        &format!(
            "{scope} Estimated cost {}. {}",
            if p.spend.by_month {
                "by local calendar month"
            } else {
                "by week, counted back from now"
            },
            if p.audience.is_public() { "Figures are rounded independently to the nearest dollar; displayed rows may not add up." } else { "Each column adds up to the estimated API cost." }
        ),
        &spend_panel(p),
    ));
    s.push_str(&section(
        "limits",
        "How often I exhaust a window limit",
        &format!("{scope} Closed windows of each plan's own limits; pools are left out."),
        &limits_panel(p),
    ));
    s
}

pub fn render(p: &Page) -> String {
    let mut s = String::with_capacity(64 * 1024);
    s.push_str("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n");
    s.push_str("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n");
    s.push_str("<meta name=\"color-scheme\" content=\"light dark\">\n");
    s.push_str("<meta name=\"referrer\" content=\"no-referrer\">\n");
    s.push_str("<title>Usage habits</title>\n<style>\n");
    s.push_str(CSS);
    s.push_str("</style>\n</head>\n<body>\n<header>\n<h1>Usage habits</h1>\n");
    let provenance = if p.facts.requests == 0 {
        "The store is empty.".to_string()
    } else {
        format!(
            "{} sessions and {} requests{}. Store read {}, local time {}.",
            fmt_int(p.facts.sessions),
            fmt_int(p.facts.requests),
            match &p.first_label {
                Some(f) => format!(" since {}", esc(f)),
                None => String::new(),
            },
            esc(&p.now_label),
            esc(&p.tz_label)
        )
    };
    let _ = writeln!(s, "<p class=\"provenance\">{provenance}</p>");
    if p.audience.is_public() {
        let _ = writeln!(
            s,
            "<p class=\"note\">De-identified export. Generated {}. Window: {}. Dollar figures are rounded to the nearest dollar.</p>",
            esc(&p.now_label),
            p.window.label()
        );
    }
    s.push_str(&controls(p));
    s.push_str("</header>\n<main>\n");
    let note = format!(
        "{} Shade is uncached input plus output tokens{}.",
        window_sentence(p),
        match p.tool {
            Some(t) => format!(" for {t}"),
            None => String::new(),
        }
    );
    s.push_str(&section(
        "activity",
        "Activity by weekday and hour",
        &note,
        &activity(p),
    ));
    let cmd = match p.window.since_arg() {
        Some(a) => format!("query summary --since {a}"),
        None => "query summary".to_string(),
    };
    let note = match p.tool {
        Some(t) => {
            format!("Same window. The {t} row matches the {t} rows of <code>{cmd}</code> added up.")
        }
        None => format!("Same window. The total row matches <code>{cmd}</code>."),
    };
    s.push_str(&section("totals", "Estimated API cost", &note, &totals(p)));
    s.push_str(&panels(p));
    s.push_str("</main>\n<footer>\n<p>");
    let _ = write!(
        s,
        "Costs are estimates at LiteLLM list prices{}. Data loaded in {:.0} ms.",
        match &p.rates_fetched_at {
            Some(t) => format!(", rates fetched {}", esc(t)),
            None => String::new(),
        },
        p.load_ms
    );
    s.push_str("</p>\n</footer>\n<script>\n");
    s.push_str(JS);
    s.push_str("</script>\n</body>\n</html>\n");
    s
}

/// The page when there is no store file yet.
pub fn render_missing(db_path: &str) -> String {
    let mut s = String::new();
    s.push_str("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n");
    s.push_str("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n");
    s.push_str("<meta name=\"color-scheme\" content=\"light dark\">\n");
    s.push_str("<title>Usage habits</title>\n<style>\n");
    s.push_str(CSS);
    s.push_str("</style>\n</head>\n<body>\n<header>\n<h1>Usage habits</h1>\n");
    let _ = write!(
        s,
        "<p class=\"provenance\">No store at <code>{}</code>.</p>\n</header>\n<main>\n<p class=\"empty\">No usage recorded yet. Run <code>usage-habit-report scan</code> to read your logs, then reload.</p>\n</main>\n</body>\n</html>\n",
        esc(db_path)
    );
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_session_tables_link_repositories_and_private_tables_keep_paths() {
        let store = crate::serve::data::tests::panel_fixture();
        let mut page = super::super::routes::load_page(
            &store.conn,
            super::super::routes::Params {
                window: Some(Window::All),
                tool: None,
            },
            crate::serve::data::tests::NOW,
            &chrono::Local,
            None,
        )
        .unwrap();
        for line in page
            .sessions
            .longest
            .iter_mut()
            .chain(&mut page.sessions.costliest)
        {
            line.project = Some("/home/demo/public-checkout".into());
            line.project_name = Some("public-repo".into());
            line.project_url = Some("https://github.com/demo/public-repo".into());
        }
        for rows in [&page.sessions.longest, &page.sessions.costliest] {
            assert!(!rows.is_empty());
            let local = session_table(&page, "fixture", rows);
            assert!(
                local.contains("<span title=\"/home/demo/public-checkout\">public-repo</span>")
            );
            assert!(!local.contains("https://github.com/"));
        }
        page.audience = Audience::Public {
            windows: Window::ALL.to_vec(),
        };
        for rows in [&page.sessions.longest, &page.sessions.costliest] {
            let public = session_table(&page, "fixture", rows);
            assert!(public.contains(
                "<a href=\"https://github.com/demo/public-repo\" rel=\"noopener\">public-repo</a>"
            ));
            assert!(!public.contains("/home/demo/"));
        }
    }

    #[test]
    fn number_formats() {
        assert_eq!(fmt_int(0), "0");
        assert_eq!(fmt_int(999), "999");
        assert_eq!(fmt_int(1000), "1,000");
        assert_eq!(fmt_int(1234567), "1,234,567");
        assert_eq!(fmt_int(-1234), "-1,234");
        assert_eq!(fmt_usd(Some(1.005), 0), "$1.00");
        assert_eq!(fmt_usd(Some(2.5), 3), "$2.50*");
        assert_eq!(fmt_usd(None, 3), "-");
    }

    #[test]
    fn escapes() {
        assert_eq!(esc("a<b>&\"c'"), "a&lt;b&gt;&amp;&quot;c&#39;");
    }
}
