//! Figures for the value page, in the radar's grammar: inline SVG in a
//! `<figure>` with a caption, a `<title>` and `<desc>`, real values in
//! labels, and the tool's hue only.
use super::html::esc;
use super::value_html::ValuePage;
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
        "<figure class=\"value-fig value-squares\"><figcaption><h3>What each subscription buys</h3><p>Each block is one month's price. The solid block is what you pay. The outlined blocks are what the rest of a month's use would cost at API list prices, at the pace of the last {} days. Block areas are to scale.</p></figcaption><svg viewBox=\"0 0 {width:.0} {height:.0}\" role=\"img\" aria-labelledby=\"squares-title squares-desc\"><title id=\"squares-title\">Plan price against est. cost at API prices</title><desc id=\"squares-desc\">{}. All {} plans: {} a month buys est. {}.</desc>",
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
