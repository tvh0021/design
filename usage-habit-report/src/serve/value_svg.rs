//! Figures for the value page, in the radar's grammar: inline SVG in a
//! `<figure>` with a caption, a `<title>` and `<desc>`, real values in
//! labels, and the tool's hue only.
use super::html::esc;
use super::value_html::ValuePage;
use crate::value::{DRIFT_MIN_RUNS, ToolValue, WindowEstimate, dollars};
use std::fmt::Write;

/// Height of the tallest stack of blocks.
const HEIGHT: f64 = 170.0;
/// Space between blocks.
const GAP: f64 = 3.0;
/// Space between plan columns.
const COL_GAP: f64 = 36.0;
/// Rough advance of one 12px monospace character, for sizing columns.
const CHAR: f64 = 7.4;
/// Most blocks one plan draws; a larger multiple is in its label.
const MAX_BLOCKS: f64 = 60.0;

/// One stack of blocks per plan, each block one month's price: the solid
/// block is what the plan costs, the outlined ones the rest of what the same
/// use would cost at API list prices, at the recent pace. Block area is
/// dollars on one scale for every plan, and the stacks sit in equal slots so
/// the plans read as peers.
pub(super) fn value_squares(p: &ValuePage) -> String {
    let r = &p.report;
    let plans: Vec<_> = r
        .tools
        .iter()
        .filter_map(|t| {
            // The tools the combined pace counts, at the list price over the
            // same days, so a plan added partway counts for the days it ran.
            let pace = crate::value::paced(t)?;
            let price = pace.price_usd?;
            let cost = pace.monthly_cost_usd?.max(0.0);
            // Unrounded, as the public page rounds the cost but not the multiple.
            let times = pace.multiple.unwrap_or(cost / price);
            Some((t, price, cost, times, pace))
        })
        .collect();
    if plans.is_empty() {
        return String::new();
    }
    // Blocks per plan, never fewer than the one paid for nor more than fit.
    let blocks = |times: f64| times.clamp(1.0, MAX_BLOCKS);
    let most = plans
        .iter()
        .map(|(_, _, _, times, _)| blocks(*times))
        .fold(0.0, f64::max);
    let cols = (most.sqrt().ceil() as usize).max(5);
    let rows = |n: f64| (n / cols as f64).ceil().max(1.0);
    // Block side is scale * sqrt(price), so area is dollars; the scale is the
    // largest that keeps every stack within HEIGHT.
    let scale = plans
        .iter()
        .map(|(_, price, _, times, _)| ((HEIGHT + GAP) / rows(blocks(*times)) - GAP) / price.sqrt())
        .fold(f64::INFINITY, f64::min)
        .max(0.0);
    let cell = |price: f64| scale * price.sqrt();
    let grid_w = |price: f64| cols as f64 * (cell(price) + GAP) - GAP;

    let labels: Vec<_> = plans
        .iter()
        .map(|(t, price, cost, times, pace)| {
            let short = (pace.days < r.pace_days as f64 - 0.5)
                .then(|| format!("from {:.0} days of use", pace.days));
            let buys = format!("{} buys {}", p.usd(Some(*price)), p.usd(Some(*cost)));
            let paid = pace
                .paid_usd
                .filter(|paid| (paid - price).abs() >= 0.005)
                .map(|paid| format!("you pay {}", p.usd(Some(paid))));
            let mult = if *times > MAX_BLOCKS {
                format!("{times:.1}x the price, {MAX_BLOCKS:.0} drawn")
            } else {
                format!("{times:.1}x the price")
            };
            (t, buys, mult, short, paid)
        })
        .collect();
    let slot = plans
        .iter()
        .zip(&labels)
        .map(|((t, price, ..), (_, buys, mult, short, _))| {
            let chars = [
                t.tool.len(),
                buys.len(),
                mult.len(),
                short.as_ref().map_or(0, String::len),
            ];
            grid_w(*price).max(*chars.iter().max().unwrap_or(&0) as f64 * CHAR)
        })
        .fold(0.0, f64::max)
        + COL_GAP;
    let base = HEIGHT + 6.0;
    let width = slot * plans.len() as f64 - COL_GAP;
    let paid_lines = labels.iter().any(|l| l.4.is_some());
    let height = base + if paid_lines { 88.0 } else { 72.0 };

    let c = &r.combined;
    let desc = labels
        .iter()
        .map(|(t, buys, mult, _, paid)| {
            format!(
                "{}: {buys} a month, {mult}{}",
                t.tool,
                paid.as_ref().map_or(String::new(), |p| format!(", {p}"))
            )
        })
        .collect::<Vec<_>>()
        .join("; ");
    let mut out = format!(
        "<figure class=\"chart-fig value-squares\"><figcaption><h3>What each subscription buys</h3><p>Each block is one month of a tool's plans at list price, over the last {} days. The solid block is the plan's list price; where that differs from what you pay, the figure below says so. The outlined blocks are what the rest of a month's use would cost at API list prices, at the pace of those days. Block areas are to scale.</p></figcaption><svg viewBox=\"0 0 {width:.0} {height:.0}\" role=\"img\" aria-labelledby=\"squares-title squares-desc\"><title id=\"squares-title\">Plan price against est. cost at API prices</title><desc id=\"squares-desc\">{}. All {} plans: {} a month at list prices buys est. {}.</desc>",
        r.pace_days,
        esc(&desc),
        c.pace_plans,
        p.usd(Some(c.pace_price_usd.unwrap_or(c.monthly_usd))),
        p.usd(c.pace_monthly_cost_usd),
    );
    for (i, ((_, price, _, times, _), (t, buys, mult, short, paid))) in
        plans.iter().zip(&labels).enumerate()
    {
        let mid = i as f64 * slot + (slot - COL_GAP) / 2.0;
        let (side, n) = (cell(*price), blocks(*times));
        let left = mid - grid_w(*price) / 2.0;
        let _ = write!(
            out,
            "<g data-tool=\"{tool}\"><title>{tool}: {buys} at API prices, {mult}{note}</title>",
            tool = esc(&t.tool),
            note = short.as_deref().map_or(String::new(), |s| format!(", {s}")),
        );
        // Fill rows from the bottom, left to right; the last block is cut to
        // the fraction of a month's price that remains.
        let whole = n.floor() as usize;
        let part = n - whole as f64;
        for k in 0..whole + usize::from(part > 0.05) {
            let (row, col) = (k / cols, k % cols);
            let w = if k < whole { side } else { side * part };
            let _ = write!(
                out,
                "<rect class=\"{}\" x=\"{:.1}\" y=\"{:.1}\" width=\"{w:.1}\" height=\"{side:.1}\"/>",
                if k == 0 { "sq-price" } else { "sq-cost" },
                left + col as f64 * (side + GAP),
                base - (row + 1) as f64 * (side + GAP) + GAP,
            );
        }
        let _ = write!(
            out,
            "<text class=\"sq-name\" x=\"{mid:.1}\" y=\"{:.1}\">{}</text><text class=\"sq-label\" x=\"{mid:.1}\" y=\"{:.1}\">{buys}</text><text class=\"sq-mult\" x=\"{mid:.1}\" y=\"{:.1}\">{mult}</text>",
            base + 18.0,
            esc(&t.tool),
            base + 34.0,
            base + 50.0,
        );
        let mut y = base + 66.0;
        for line in [paid, short].into_iter().flatten() {
            let _ = write!(
                out,
                "<text class=\"sq-note\" x=\"{mid:.1}\" y=\"{y:.1}\">{line}</text>"
            );
            y += 16.0;
        }
        out.push_str("</g>");
    }
    out.push_str("</svg></figure>");
    out
}

/// A window's id within its tool: its length, then its place among the
/// tool's windows when another plan's window of that length comes first.
fn window_key(t: &ToolValue, w: &WindowEstimate) -> String {
    let i = t
        .windows
        .iter()
        .position(|x| std::ptr::eq(x, w))
        .unwrap_or(0);
    if t.windows[..i]
        .iter()
        .any(|x| x.window == w.window && x.pool == w.pool)
    {
        format!("{}-{i}", w.window)
    } else {
        w.window.clone()
    }
}

const DRIFT_W: f64 = 720.0;
const DRIFT_PLOT: f64 = 150.0;

/// One window of a toggled figure: the window, its name, and for
/// [`drift_chart`] the sentence that reads its drift.
pub(super) struct WindowPanel<'a> {
    pub window: &'a WindowEstimate,
    pub name: String,
    pub sentence: Option<String>,
}

/// What one full window held each month, twice: at that month's own prices
/// (solid, in the tool's hue) and at one reference model's prices (dashed,
/// ink), so a change in prices reads apart from a change in the limit.
/// Months resting on fewer than `DRIFT_MIN_RUNS` runs are drawn hollow.
/// Several windows share one figure, with a radio toggle between them that
/// needs no script.
pub(super) fn drift_chart(t: &ToolValue, panels: &[WindowPanel]) -> String {
    let drawn: Vec<_> = panels
        .iter()
        .filter_map(|p| Some((p, drift_svg(t, p.window, &p.name)?)))
        .collect();
    if drawn.is_empty() {
        return String::new();
    }
    let key = match t.reference_model.as_deref() {
        Some(m) => format!(
            "Solid: what a full window held at each month's own prices. Dashed: the same use at {} prices, so a change in prices reads apart from a change in the limit.",
            esc(m)
        ),
        None => "What a full window held at each month's own prices.".to_owned(),
    };
    let drawn = drawn
        .into_iter()
        .map(|(p, svg)| {
            let sentence = p
                .sentence
                .as_ref()
                .map(|s| format!("<p class=\"dr-sentence\">{}</p>", esc(s)))
                .unwrap_or_default();
            (p, format!("{sentence}{svg}"))
        })
        .collect::<Vec<_>>();
    toggled_figure(
        "drift",
        t,
        |w| format!("What a full {w}window holds, month by month"),
        &format!("{key} A hollow dot rests on fewer than {DRIFT_MIN_RUNS} full windows."),
        &drawn,
    )
}

/// Panels `dashboard.css` has radio-tab rules for.
const TABS: usize = 8;

/// A figure of one chart per window. With more than one, a radio toggle
/// picks the window shown, in CSS alone. `heading` gets the window's name
/// and a space when there is only one, an empty string otherwise. Panels
/// past the first [`TABS`] have no tab rules, so they follow the tabbed one,
/// always shown under their own name.
fn toggled_figure(
    class: &str,
    t: &ToolValue,
    heading: impl Fn(&str) -> String,
    caption: &str,
    panels: &[(&WindowPanel, String)],
) -> String {
    let tool = esc(&t.tool);
    let mut inputs = String::new();
    let mut labels = String::new();
    let mut bodies = String::new();
    for (i, (p, body)) in panels.iter().enumerate() {
        if i >= TABS {
            let _ = write!(
                bodies,
                "<div class=\"panel-more\"><h4>{}</h4>{body}</div>",
                esc(&super::value_html::capitalised(&p.name))
            );
            continue;
        }
        let id = format!("{class}-{tool}-{}", window_key(t, p.window));
        if panels.len() > 1 {
            let _ = write!(
                inputs,
                "<input type=\"radio\" class=\"sr\" name=\"{class}-{tool}\" id=\"{id}\"{}>",
                if i == 0 { " checked" } else { "" }
            );
            let _ = write!(
                labels,
                "<label for=\"{id}\">{}</label>",
                esc(&super::value_html::capitalised(&p.name))
            );
        }
        let _ = write!(bodies, "<div class=\"panel\">{body}</div>");
    }
    let toggle = if labels.is_empty() {
        String::new()
    } else {
        format!("<div class=\"toggle\">{labels}</div>")
    };
    let heading = match panels {
        [(p, _)] => heading(&format!("{} ", esc(&p.name))),
        _ => heading(""),
    };
    format!(
        "<figure class=\"chart-fig toggled {class}\" data-tool=\"{tool}\">{inputs}<figcaption><h3>{heading}</h3>{toggle}<p>{caption}</p></figcaption>{bodies}</figure>"
    )
}

/// The chart of one window for [`drift_chart`], or `None` with fewer than
/// two priced months.
fn drift_svg(t: &ToolValue, w: &WindowEstimate, window: &str) -> Option<String> {
    let points: Vec<_> = w
        .drift
        .iter()
        .filter_map(|d| Some((d, d.per_window.estimated_cost_usd?)))
        .collect();
    if points.len() < 2 {
        return None;
    }
    let reference = t.reference_model.as_deref();
    let peak = points
        .iter()
        .flat_map(|(d, own)| [Some(*own), d.per_window.reference_cost_usd])
        .flatten()
        .fold(0.0, f64::max);
    if peak <= 0.0 {
        return None;
    }
    let top = 24.0;
    let base = top + DRIFT_PLOT;
    let y = |v: f64| base - DRIFT_PLOT * v / peak;
    let step = DRIFT_W / points.len() as f64;
    let x = |i: usize| (i as f64 + 0.5) * step;
    let line = |vals: &dyn Fn(usize) -> Option<f64>| {
        (0..points.len())
            .filter_map(|i| Some(format!("{:.1},{:.1}", x(i), y(vals(i)?))))
            .collect::<Vec<_>>()
            .join(" ")
    };
    let mut body = String::new();
    let _ = write!(
        body,
        "<line class=\"dr-base\" x1=\"0\" x2=\"{DRIFT_W}\" y1=\"{base:.1}\" y2=\"{base:.1}\"/>"
    );
    if reference.is_some() {
        let _ = write!(
            body,
            "<polyline class=\"dr-ref\" points=\"{}\"/>",
            line(&|i| points[i].0.per_window.reference_cost_usd)
        );
    }
    let _ = write!(
        body,
        "<polyline class=\"dr-own\" points=\"{}\"/>",
        line(&|i| Some(points[i].1))
    );
    let mut desc = Vec::new();
    for (i, (d, own)) in points.iter().enumerate() {
        let (cx, cy) = (x(i), y(*own));
        let month = month_abbr(&d.month);
        let refc = d
            .per_window
            .reference_cost_usd
            .filter(|_| reference.is_some());
        let mut tip = format!(
            "{month}: {} at its own prices, from {} full window{}",
            dollars(*own),
            d.runs,
            if d.runs == 1 { "" } else { "s" }
        );
        if let (Some(r), Some(m)) = (refc, reference) {
            tip.push_str(&format!(", {} at {m} prices", dollars(r)));
        }
        let _ = write!(
            body,
            "<g><title>{}</title><circle class=\"dr-dot{}\" cx=\"{cx:.1}\" cy=\"{cy:.1}\" r=\"5\"/>",
            esc(&tip),
            if d.runs < DRIFT_MIN_RUNS {
                " hollow"
            } else {
                ""
            }
        );
        // The higher of the two values is labelled above, the other below.
        let own_above = refc.is_none_or(|r| *own >= r);
        let _ = write!(
            body,
            "<text class=\"dr-value\" x=\"{cx:.1}\" y=\"{:.1}\">{}</text>",
            if own_above { cy - 11.0 } else { cy + 20.0 },
            dollars(*own)
        );
        if let Some(r) = refc {
            let ry = y(r);
            let _ = write!(
                body,
                "<circle class=\"dr-ring\" cx=\"{cx:.1}\" cy=\"{ry:.1}\" r=\"4\"/><text class=\"dr-ref-value\" x=\"{cx:.1}\" y=\"{:.1}\">{}</text>",
                if own_above { ry + 19.0 } else { ry - 10.0 },
                dollars(r)
            );
        }
        let _ = write!(
            body,
            "<text class=\"dr-month\" x=\"{cx:.1}\" y=\"{:.1}\">{}</text></g>",
            base + 18.0,
            esc(&month)
        );
        desc.push(tip);
    }
    let height = base + 28.0;
    Some(format!(
        "<svg viewBox=\"0 0 {DRIFT_W} {height:.0}\" role=\"img\" aria-labelledby=\"drift-{id}-title drift-{id}-desc\"><title id=\"drift-{id}-title\">{tool}: est. cost of a full {window} window by month</title><desc id=\"drift-{id}-desc\">{}</desc>{body}</svg>",
        esc(&desc.join("; ")),
        tool = esc(&t.tool),
        window = esc(window),
        id = format!("{}-{}", esc(&t.tool), window_key(t, w)),
    ))
}

/// "Jun" from "2026-06"; the key itself when it does not parse.
pub(super) fn month_abbr(key: &str) -> String {
    const NAMES: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    key.split_once('-')
        .and_then(|(_, m)| m.parse::<usize>().ok())
        .and_then(|m| NAMES.get(m.checked_sub(1)?))
        .map_or_else(|| key.to_owned(), |n| (*n).to_owned())
}

const MODEL_LABEL: f64 = 150.0;
const MODEL_PLOT: f64 = 400.0;
const MODEL_ROW: f64 = 30.0;

/// What a full window was worth at list prices, model by model: one row per
/// model that did most of the work in at least `DRIFT_MIN_RUNS` full
/// windows, a dot at the median and a line across the range. Equal dots
/// would mean the limit is a dollar budget; spread dots show which models
/// get more out of it.
pub(super) fn model_chart(t: &ToolValue, panels: &[WindowPanel]) -> String {
    let drawn: Vec<_> = panels
        .iter()
        .filter_map(|p| Some((p, model_svg(t, p.window, &p.name)?)))
        .collect();
    if drawn.is_empty() {
        return String::new();
    }
    toggled_figure(
        "model-windows",
        t,
        |w| format!("What a full {w}window is worth, by model"),
        "Dot: the typical window. Line: lowest to highest.",
        &drawn,
    )
}

/// The chart of one window for [`model_chart`], or `None` with fewer than
/// two models of `DRIFT_MIN_RUNS` full windows.
fn model_svg(t: &ToolValue, w: &WindowEstimate, window: &str) -> Option<String> {
    let rows: Vec<_> = w
        .by_model
        .iter()
        .filter(|m| m.windows >= DRIFT_MIN_RUNS)
        .collect();
    if rows.len() < 2 {
        return None;
    }
    let mut rows = rows;
    rows.sort_by(|a, b| b.median_cost_usd.total_cmp(&a.median_cost_usd));
    let peak = rows.iter().map(|m| m.max_cost_usd).fold(0.0, f64::max);
    if peak <= 0.0 {
        return None;
    }
    let x = |v: f64| MODEL_LABEL + MODEL_PLOT * v / peak;
    let mut body = String::new();
    let mut desc = Vec::new();
    for (i, m) in rows.iter().enumerate() {
        let mid = 14.0 + i as f64 * MODEL_ROW;
        let tip = format!(
            "{}: {} a full {window} window (median of {}), {} to {}",
            m.model,
            dollars(m.median_cost_usd),
            m.windows,
            dollars(m.min_cost_usd),
            dollars(m.max_cost_usd)
        );
        let _ = write!(
            body,
            "<g><title>{}</title><text class=\"mw-model\" x=\"0\" y=\"{:.1}\">{}</text><line class=\"mw-range\" x1=\"{:.1}\" x2=\"{:.1}\" y1=\"{mid:.1}\" y2=\"{mid:.1}\"/><circle class=\"mw-dot\" cx=\"{:.1}\" cy=\"{mid:.1}\" r=\"6\"/><text class=\"mw-value\" x=\"{:.1}\" y=\"{:.1}\">{}</text><text class=\"mw-note\" x=\"{:.1}\" y=\"{:.1}\">{} windows</text></g>",
            esc(&tip),
            mid + 4.0,
            esc(&m.model),
            x(m.min_cost_usd),
            x(m.max_cost_usd).max(x(m.min_cost_usd) + 1.0),
            x(m.median_cost_usd),
            MODEL_LABEL + MODEL_PLOT + 20.0,
            mid + 4.0,
            dollars(m.median_cost_usd),
            MODEL_LABEL + MODEL_PLOT + 70.0,
            mid + 4.0,
            m.windows
        );
        desc.push(tip);
    }
    let base = 14.0 + rows.len() as f64 * MODEL_ROW - 8.0;
    let _ = write!(
        body,
        "<line class=\"dr-base\" x1=\"{MODEL_LABEL}\" x2=\"{:.1}\" y1=\"{base:.1}\" y2=\"{base:.1}\"/><text class=\"mw-note mw-axis\" x=\"{MODEL_LABEL}\" y=\"{:.1}\">$0</text><text class=\"mw-note mw-axis\" x=\"{:.1}\" y=\"{:.1}\">{}</text>",
        MODEL_LABEL + MODEL_PLOT,
        base + 14.0,
        MODEL_LABEL + MODEL_PLOT,
        base + 14.0,
        dollars(peak)
    );
    let height = base + 20.0;
    Some(format!(
        "<svg viewBox=\"0 0 720 {height:.0}\" role=\"img\" aria-labelledby=\"mw-{id}-title mw-{id}-desc\"><title id=\"mw-{id}-title\">{tool}: est. cost of a full {window} window by main model</title><desc id=\"mw-{id}-desc\">{}</desc>{body}</svg>",
        esc(&desc.join("; ")),
        tool = esc(&t.tool),
        window = esc(window),
        id = format!("{}-{}", esc(&t.tool), window_key(t, w)),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::value::ModelWindow;

    #[test]
    fn every_tab_of_a_two_plan_tool_shows_its_panel_and_plan_names_are_escaped() {
        let mw = |m: &str| ModelWindow {
            model: m.into(),
            windows: 3,
            median_cost_usd: 2.0,
            min_cost_usd: 1.0,
            max_cost_usd: 3.0,
        };
        let w = |plan: &str, window: &str| WindowEstimate {
            plan: plan.into(),
            window: window.into(),
            window_hours: 5,
            by_model: vec![mw("a"), mw("b")],
            ..Default::default()
        };
        let t = ToolValue {
            tool: "codex".into(),
            windows: vec![
                w("<Plus>", "session"),
                w("<Plus>", "weekly"),
                w("Pro", "session"),
                w("Pro", "weekly"),
            ],
            ..Default::default()
        };
        let panels: Vec<WindowPanel> = t
            .windows
            .iter()
            .map(|w| WindowPanel {
                window: w,
                name: format!("{}, {}", w.plan, w.window),
                sentence: None,
            })
            .collect();
        let html = model_chart(&t, &panels);
        assert!(!html.contains("<Plus>"), "{html}");
        assert!(html.contains("<title id=\"mw-codex-session-title\">codex: est. cost of a full &lt;Plus&gt;, session window"));
        let ids: std::collections::BTreeSet<&str> = html
            .split("<input type=\"radio\"")
            .skip(1)
            .filter_map(|i| i.split("id=\"").nth(1)?.split('"').next())
            .collect();
        assert_eq!(ids.len(), 4, "{ids:?}");
        assert_eq!(html.matches("panel-more").count(), 0);
        assert_eq!(html.matches("<div class=\"panel\">").count(), 4);
        let css = super::super::html::CSS;
        for n in 1..=ids.len() {
            for rule in [
                format!(
                    "input:nth-of-type({n}):checked ~ figcaption .toggle label:nth-of-type({n})"
                ),
                format!(
                    "input:nth-of-type({n}):focus-visible ~ figcaption .toggle label:nth-of-type({n})"
                ),
            ] {
                assert!(css.contains(&rule), "{rule}");
            }
            if n > 1 {
                let show = format!("input:nth-of-type({n}):checked ~ .panel:nth-of-type({n})");
                assert!(css.contains(&show), "{show}");
            }
        }
    }

    #[test]
    fn panels_past_the_tab_rules_are_shown_untabbed() {
        let w = |n: usize| WindowEstimate {
            plan: format!("Plan {n}"),
            window: "session".into(),
            window_hours: 5,
            ..Default::default()
        };
        let t = ToolValue {
            tool: "codex".into(),
            windows: (1..=10).map(w).collect(),
            ..Default::default()
        };
        let made: Vec<WindowPanel> = t
            .windows
            .iter()
            .map(|w| WindowPanel {
                window: w,
                name: w.plan.clone(),
                sentence: None,
            })
            .collect();
        let panels: Vec<_> = made
            .iter()
            .map(|p| (p, format!("<svg>{}</svg>", p.name)))
            .collect();
        let html = toggled_figure("x", &t, |_| String::new(), "", &panels);
        assert_eq!(html.matches("<input type=\"radio\"").count(), TABS);
        assert_eq!(html.matches("<label ").count(), TABS);
        assert_eq!(html.matches("<div class=\"panel\">").count(), TABS);
        assert!(html.contains("<div class=\"panel-more\"><h4>Plan 9</h4><svg>Plan 9</svg></div>"));
        assert!(html.contains("<h4>Plan 10</h4>"));
        let css = super::super::html::CSS;
        assert!(css.contains(&format!(
            "input:nth-of-type({TABS}):checked ~ .panel:nth-of-type({TABS})"
        )));
        assert!(!css.contains(&format!("nth-of-type({})", TABS + 1)));
        assert!(!css.contains(".toggled .panel-more"));
    }
}
