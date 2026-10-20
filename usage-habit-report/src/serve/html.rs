//! HTML for the dashboard, built from strings. The CSS and JS are embedded so
//! the page is one self-contained document with no outside requests, which
//! is also what the public snapshot (ticket 11) needs.
//!
//! Panels added by later tickets call [`section`] for their frame and only
//! supply a body; see docs/design.md for the order and ids.

use super::data::{Heatmap, StoreFacts, TOOLS, Totals, WEEKDAYS, Window};
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
    s.push_str("<!-- ticket 10 panels: #models #agents #projects #sessions #cache #spend -->\n");
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
