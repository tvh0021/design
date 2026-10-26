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
            let pace = t.pace.as_ref()?;
            let price = t.subscription.monthly_usd.filter(|v| *v > 0.0)?;
            Some((t, price, pace.monthly_cost_usd?.max(0.0), pace))
        })
        .collect();
    if plans.is_empty() {
        return String::new();
    }
    // Blocks per plan, never fewer than the one paid for.
    let blocks = |price: f64, cost: f64| (cost / price).max(1.0);
    let most = plans
        .iter()
        .map(|(_, price, cost, _)| blocks(*price, *cost))
        .fold(0.0, f64::max);
    let cols = (most.sqrt().ceil() as usize).max(5);
    let rows = |n: f64| (n / cols as f64).ceil().max(1.0);
    // Block side is scale * sqrt(price), so area is dollars; the scale is the
    // largest that keeps every stack within HEIGHT.
    let scale = plans
        .iter()
        .map(|(_, price, cost, _)| {
            ((HEIGHT + GAP) / rows(blocks(*price, *cost)) - GAP) / price.sqrt()
        })
        .fold(f64::INFINITY, f64::min);
    let cell = |price: f64| scale * price.sqrt();
    let grid_w = |price: f64| cols as f64 * (cell(price) + GAP) - GAP;

    let labels: Vec<_> = plans
        .iter()
        .map(|(t, price, cost, pace)| {
            let short = (pace.days < r.pace_days as f64 - 0.5)
                .then(|| format!("from {:.0} days of use", pace.days));
            let buys = format!("{} buys {}", p.usd(Some(*price)), p.usd(Some(*cost)));
            (t, buys, pace.multiple.unwrap_or(0.0), short)
        })
        .collect();
    let slot = plans
        .iter()
        .zip(&labels)
        .map(|((t, price, ..), (_, buys, _, short))| {
            let chars = [
                t.tool.len(),
                buys.len(),
                short.as_ref().map_or(0, String::len),
            ];
            grid_w(*price).max(*chars.iter().max().unwrap_or(&0) as f64 * CHAR)
        })
        .fold(0.0, f64::max)
        + COL_GAP;
    let base = HEIGHT + 6.0;
    let width = slot * plans.len() as f64 - COL_GAP;
    let height = base + 72.0;

    let c = &r.combined;
    let desc = labels
        .iter()
        .map(|(t, buys, mult, _)| format!("{}: {buys} a month, {mult:.1} times the price", t.tool))
        .collect::<Vec<_>>()
        .join("; ");
    let mut out = format!(
        "<figure class=\"chart-fig value-squares\"><figcaption><h3>What each subscription buys</h3><p>Each block is one month's price. The solid block is what you pay. The outlined blocks are what the rest of a month's use would cost at API list prices, at the pace of the last {} days. Block areas are to scale.</p></figcaption><svg viewBox=\"0 0 {width:.0} {height:.0}\" role=\"img\" aria-labelledby=\"squares-title squares-desc\"><title id=\"squares-title\">Plan price against est. cost at API prices</title><desc id=\"squares-desc\">{}. All {} plans: {} a month buys est. {}.</desc>",
        r.pace_days,
        esc(&desc),
        c.plans,
        p.usd(Some(c.monthly_usd)),
        p.usd(c.pace_monthly_cost_usd),
    );
    for (i, ((_, price, cost, _), (t, buys, mult, short))) in plans.iter().zip(&labels).enumerate()
    {
        let mid = i as f64 * slot + (slot - COL_GAP) / 2.0;
        let (side, n) = (cell(*price), blocks(*price, *cost));
        let left = mid - grid_w(*price) / 2.0;
        let _ = write!(
            out,
            "<g data-tool=\"{tool}\"><title>{tool}: {buys} at API prices, {mult:.1} times the price{note}</title>",
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
            "<text class=\"sq-name\" x=\"{mid:.1}\" y=\"{:.1}\">{}</text><text class=\"sq-label\" x=\"{mid:.1}\" y=\"{:.1}\">{buys}</text><text class=\"sq-mult\" x=\"{mid:.1}\" y=\"{:.1}\">{mult:.1}x the price</text>",
            base + 18.0,
            esc(&t.tool),
            base + 34.0,
            base + 50.0,
        );
        if let Some(s) = short {
            let _ = write!(
                out,
                "<text class=\"sq-note\" x=\"{mid:.1}\" y=\"{:.1}\">{s}</text>",
                base + 66.0
            );
        }
        out.push_str("</g>");
    }
    out.push_str("</svg></figure>");
    out
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

/// A figure of one chart per window. With more than one, a radio toggle
/// picks the window shown, in CSS alone. `heading` gets the window's name
/// and a space when there is only one, an empty string otherwise.
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
        let id = format!("{class}-{tool}-{}", p.window.window);
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
        id = format!("{}-{}", esc(&t.tool), w.window),
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
        id = format!("{}-{}", esc(&t.tool), w.window),
    ))
}
