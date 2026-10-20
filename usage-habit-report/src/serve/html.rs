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

/// Everything the page shows, already computed; no database here.
#[derive(Debug, Clone)]
pub struct Page {
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
    pub projects: Projects,
    pub sessions: SessionLists,
    pub spend: Spend,
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

fn href(window: Window, tool: Option<&str>) -> String {
    match tool {
        Some(t) => format!("?window={}&amp;tool={}", window.key(), esc(t)),
        None => format!("?window={}", window.key()),
    }
}

fn controls(p: &Page) -> String {
    let mut s = String::from("<nav class=\"controls\" aria-label=\"Window and tool\">");
    s.push_str("<p class=\"group\"><span class=\"group-name\">Window</span>");
    for w in Window::ALL {
        let current = if w == p.window {
            " aria-current=\"page\""
        } else {
            ""
        };
        let _ = write!(
            s,
            " <a href=\"{}\"{current}>{}</a>",
            href(w, p.tool),
            w.label()
        );
    }
    s.push_str("</p><p class=\"group\"><span class=\"group-name\">Tool</span>");
    let current = if p.tool.is_none() {
        " aria-current=\"page\""
    } else {
        ""
    };
    let _ = write!(s, " <a href=\"{}\"{current}>all</a>", href(p.window, None));
    for t in TOOLS {
        let current = if p.tool == Some(t) {
            " aria-current=\"page\""
        } else {
            ""
        };
        let _ = write!(
            s,
            " <a class=\"tool\" data-tool=\"{t}\" href=\"{}\"{current}>{t}</a>",
            href(p.window, Some(t))
        );
    }
    s.push_str("</p></nav>\n");
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

fn cell_readout(day: usize, hour: usize, c: &super::data::Cell) -> String {
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
        fmt_usd(c.cost_usd, c.unpriced)
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
            let text = esc(&cell_readout(d, hour, c));
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

fn tokens_cells(t: &Tokens) -> String {
    format!(
        "<td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>",
        fmt_int(t.requests),
        fmt_int(t.input_tokens),
        fmt_int(t.cache_read_input_tokens),
        fmt_int(t.cache_creation_input_tokens),
        fmt_int(t.output_tokens),
        fmt_usd(t.estimated_cost_usd, t.unpriced_requests)
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
            href(p.window, Some(&row.tool)),
            tokens_cells(&row.tokens)
        );
    }
    let _ = write!(
        body,
        "<tr class=\"total\"><th scope=\"row\">Total</th>{}</tr>",
        tokens_cells(&t.total)
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
fn units(on: usize, of: usize, track: bool, tool: Option<&str>) -> String {
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
    let mut body = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger matrix\"><thead><tr><th scope=\"col\">Model</th>",
    );
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
    body.push_str("</p>");
    body
}

fn mix_table(caption: &str, entries: &[crate::agents::ModelMixEntry]) -> String {
    const ROWS: usize = 6;
    let mut s = format!(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>{caption}</caption><thead><tr><th scope=\"col\">Model</th><th scope=\"col\">Requests</th><th scope=\"col\">Share</th></tr></thead><tbody>"
    );
    if entries.is_empty() {
        s.push_str("<tr><td class=\"l zero\" colspan=\"3\">No requests.</td></tr>");
    }
    for e in entries.iter().take(ROWS) {
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{}</th><td>{}</td><td>{}</td></tr>",
            e.model
                .as_deref()
                .map_or("<span class=\"untitled\">no model</span>".into(), esc),
            fmt_int(e.requests),
            fmt_pct(Some(e.request_share))
        );
    }
    if entries.len() > ROWS {
        let rest: i64 = entries[ROWS..].iter().map(|e| e.requests).sum();
        let share: f64 = entries[ROWS..].iter().map(|e| e.request_share).sum();
        let _ = write!(
            s,
            "<tr><th scope=\"row\">{} more</th><td>{}</td><td>{}</td></tr>",
            entries.len() - ROWS,
            fmt_int(rest),
            fmt_pct(Some(share))
        );
    }
    s.push_str("</tbody></table></div>");
    s
}

fn agents_panel(p: &Page) -> String {
    let a = &p.agents;
    if a.totals.top_level_sessions == 0 && a.totals.subagent_sessions == 0 {
        return nothing_here(p);
    }
    let t = &a.totals;
    let mut body = String::from("<div class=\"facts\">");
    let _ = write!(
        body,
        "<p>{} top-level sessions; {} of them spawned subagents, {} subagent sessions in all.</p>",
        fmt_int(t.top_level_sessions as i64),
        fmt_int(t.sessions_with_subagents as i64),
        fmt_int(t.subagent_sessions as i64)
    );
    let ts = &a.subagent_token_share;
    let _ = write!(
        body,
        "<p>Subagents spent {} of tokens: {} of {} uncached input plus output tokens.</p>",
        fmt_pct(ts.share),
        fmt_int(ts.subagent_tokens),
        fmt_int(ts.total_tokens)
    );
    body.push_str("</div><div class=\"pair\">");

    // Histogram: sessions by number of subagents, count bars scaled to the
    // largest bucket.
    let most = a.histogram.iter().map(|b| b.sessions).max().unwrap_or(0);
    body.push_str("<div class=\"table-wrap\"><table class=\"ledger\"><caption>Subagents per session</caption><thead><tr><th scope=\"col\">Subagents</th><th scope=\"col\">Sessions</th><th scope=\"col\" class=\"l\"><span class=\"sr\">Bar</span></th></tr></thead><tbody>");
    for b in &a.histogram {
        let on = if most == 0 || b.sessions == 0 {
            0
        } else {
            (b.sessions * 20).div_ceil(most).clamp(1, 20)
        };
        let _ = write!(
            body,
            "<tr><th scope=\"row\">{}</th><td>{}</td><td class=\"bar\">{}</td></tr>",
            esc(&b.bucket),
            fmt_int(b.sessions as i64),
            units(on, 20, false, None)
        );
    }
    body.push_str("</tbody></table></div>");
    body.push_str("<div class=\"stack\">");
    body.push_str(&mix_table(
        "Main-thread requests by model",
        &a.model_mix.main_thread,
    ));
    body.push_str(&mix_table(
        "Subagent requests by model",
        &a.model_mix.subagent,
    ));
    body.push_str("</div></div>");

    if a.heaviest_fanouts.is_empty() {
        body.push_str("<p class=\"empty\">No session in this window spawned subagents.</p>");
    } else {
        body.push_str("<div class=\"table-wrap\"><table class=\"ledger\"><caption>Heaviest fan-outs, by most subagents running at once</caption><thead><tr><th scope=\"col\">Session</th><th scope=\"col\" class=\"l\">Tool</th><th scope=\"col\">Subagents</th><th scope=\"col\">Max concurrent</th><th scope=\"col\">Subagent tokens</th><th scope=\"col\">Share of session</th></tr></thead><tbody>");
        for f in &a.heaviest_fanouts {
            let _ = write!(
                body,
                "<tr><td class=\"title\">{}</td><td class=\"l\">{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                title_cell(f.title.as_deref()),
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
                "<p>{} {} subagent sessions have no linked parent in this window; their active time counts as separate top-level sessions. They count in the token share but not in the histogram or the fan-outs.</p>",
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
    s.push_str("</span>");
    s
}

fn all_tokens(t: &Tokens) -> i64 {
    t.input_tokens + t.cache_read_input_tokens + t.cache_creation_input_tokens + t.output_tokens
}

fn projects_panel(p: &Page) -> String {
    let pr = &p.projects;
    if pr.lines.is_empty() {
        return nothing_here(p);
    }
    let mut body = String::from(
        "<div class=\"table-wrap\"><table class=\"ledger\"><thead><tr><th scope=\"col\">Project</th><th scope=\"col\">Sessions</th><th scope=\"col\">Active time</th><th scope=\"col\">Tokens</th><th scope=\"col\">Est. cost</th><th scope=\"col\" class=\"l\">Tools</th></tr></thead><tbody>",
    );
    for l in &pr.lines {
        let r = &l.row;
        let name = match (&r.project, &r.name) {
            (Some(path), Some(name)) => {
                format!("<span title=\"{}\">{}</span>", esc(path), esc(name))
            }
            _ => "<span class=\"untitled\">no known directory</span>".into(),
        };
        let _ = write!(
            body,
            "<tr><th scope=\"row\">{name}</th><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class=\"bar\">{}</td></tr>",
            fmt_int(r.sessions),
            fmt_elapsed(l.active_seconds),
            fmt_int(all_tokens(&r.tokens)),
            fmt_usd(r.tokens.estimated_cost_usd, r.tokens.unpriced_requests),
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
    foot.push_str("Hover a name for its full path, a tool mix for its shares.");
    if pr.lines.iter().any(|l| l.row.tokens.unpriced_requests > 0) {
        foot.push_str(" * leaves out requests whose model has no price.");
    }
    let _ = write!(body, "<p class=\"note\">{foot}</p>");
    body
}

fn session_table(caption: &str, rows: &[SessionLine]) -> String {
    let mut s = format!(
        "<div class=\"table-wrap\"><table class=\"ledger\"><caption>{caption}</caption><thead><tr><th scope=\"col\">Session</th><th scope=\"col\" class=\"l\">Tool</th><th scope=\"col\" class=\"l\">Project</th><th scope=\"col\">Active time</th><th scope=\"col\">Requests</th><th scope=\"col\">Est. cost</th></tr></thead><tbody>"
    );
    for r in rows {
        let project = match (&r.project, &r.project_name) {
            (Some(path), Some(name)) => {
                format!("<span title=\"{}\">{}</span>", esc(path), esc(name))
            }
            (Some(path), None) => esc(&crate::project::project_name(path)),
            _ => "<span class=\"untitled\">none</span>".into(),
        };
        let _ = write!(
            s,
            "<tr><td class=\"title\">{}</td><td class=\"l\">{}</td><td class=\"l\">{project}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            title_cell(r.title.as_deref()),
            tool_name(&r.tool),
            fmt_elapsed(r.active_seconds),
            fmt_int(r.requests),
            fmt_usd(r.cost_usd, r.unpriced)
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
    let mut body = String::from("<div class=\"stack\">");
    body.push_str(&session_table("Longest, by active time", &sl.longest));
    if sl.costliest.is_empty() {
        body.push_str("<p class=\"empty\">No priced requests in this window. Run <code>usage-habit-report rates update</code>, then any <code>query</code>, to price them.</p>");
    } else {
        body.push_str(&session_table(
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

fn spend_panel(p: &Page) -> String {
    let sp = &p.spend;
    if p.totals.total.requests == 0 || sp.periods.is_empty() {
        return nothing_here(p);
    }
    let period = if sp.by_month { "Month" } else { "Week" };
    let mut body = format!(
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
    let mut col_totals = vec![(None::<f64>, 0i64); sp.tools.len()];
    for per in &sp.periods {
        let _ = write!(body, "<tr><th scope=\"row\">{}</th>", esc(&per.label));
        let mut row = (None::<f64>, 0i64);
        for (i, (cost, unpriced)) in per.cells.iter().enumerate() {
            let _ = write!(body, "<td>{}</td>", fmt_usd(*cost, *unpriced));
            for acc in [&mut row, &mut col_totals[i]] {
                if let Some(c) = cost {
                    *acc.0.get_or_insert(0.0) += c;
                }
                acc.1 += unpriced;
            }
        }
        if multi {
            let _ = write!(body, "<td>{}</td>", fmt_usd(row.0, row.1));
        }
        body.push_str("</tr>");
    }
    body.push_str("<tr class=\"total\"><th scope=\"row\">Total</th>");
    let mut all = (None::<f64>, 0i64);
    for (cost, unpriced) in &col_totals {
        let _ = write!(body, "<td>{}</td>", fmt_usd(*cost, *unpriced));
        if let Some(c) = cost {
            *all.0.get_or_insert(0.0) += c;
        }
        all.1 += unpriced;
    }
    if multi {
        let _ = write!(body, "<td>{}</td>", fmt_usd(all.0, all.1));
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
        "Multi-agent habit",
        &format!(
            "{scope} Tokens are uncached input plus output. Figures match <code>query agents{since}</code>{}.",
            tool_rows("row of by_tool")
        ),
        &agents_panel(p),
    ));
    let idle_minutes = crate::store::IDLE_GAP_SECONDS / 60;
    s.push_str(&section(
        "projects",
        "Time and cost by project",
        &format!(
            "{scope} Sessions, tokens and cost match <code>query projects{since}</code>{}. Active time adds up top-level sessions after removing gaps longer than {idle_minutes} minutes. Linked child activity bridges their parent's gaps; unlinked children count separately.",
            match p.tool {
                Some(t) => format!(" counting {t} only"),
                None => String::new(),
            }
        ),
        &projects_panel(p),
    ));
    s.push_str(&section(
        "sessions",
        "Longest and costliest sessions",
        &format!(
            "{scope} Top-level sessions with a request in the window. Active time covers the whole session and its linked children; requests and cost count its own requests in the window, as <code>query session &lt;id&gt;{since}</code> does. Titles are the first line of the opening prompt."
        ),
        &sessions_panel(p),
    ));
    s.push_str(&section(
        "cache",
        "Cache-read ratio",
        &format!(
            "{scope} Cache-read tokens as a share of all input: uncached, cache read and cache write. Same figures as the window totals."
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
            "{scope} Estimated cost {}. Each column adds up to the cost in the window totals.",
            if p.spend.by_month {
                "by local calendar month"
            } else {
                "by week, counted back from now"
            }
        ),
        &spend_panel(p),
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
    s.push_str(&section("totals", "Window totals", &note, &totals(p)));
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
