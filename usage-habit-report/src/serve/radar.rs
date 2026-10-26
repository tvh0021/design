//! Small multiples of model task mix, rendered as offline SVG.
use super::data::{MatrixRow, ModelMatrix};
use super::html::{esc, fmt_int};
use std::{cmp::Reverse, fmt::Write};

fn provider(model: Option<&str>) -> (&'static str, &'static str) {
    match model.unwrap_or("") {
        m if m.starts_with("claude-") => ("anthropic", "Anthropic"),
        m if m.starts_with("gpt-") || m.starts_with("o3") || m.starts_with("o4") => {
            ("openai", "OpenAI")
        }
        m if m.starts_with("gemini-") => ("google", "Google"),
        _ => ("unknown", "Other / unknown provider"),
    }
}

/// Provider groups, then newest major generation and model tier. Minor
/// versions break ties within a tier; unknown families follow named tiers.
fn model_order(model: Option<&str>) -> (u8, Reverse<u32>, u8, Reverse<Vec<u32>>) {
    let provider = match provider(model).0 {
        "anthropic" => 0,
        "openai" => 1,
        "google" => 2,
        _ => 3,
    };
    let parts = model.unwrap_or("").split('-').collect::<Vec<_>>();
    let family = if provider == 0 {
        parts.get(1)
    } else {
        parts.get(2)
    }
    .copied();
    let tier = match (provider, family) {
        (0, Some("fable")) | (1, Some("astra")) | (2, Some("argon")) => 0,
        (0, Some("opus")) | (1, Some("sol")) | (2, Some("pro")) => 1,
        (0, Some("sonnet")) | (1, Some("terra")) | (2, Some("flash")) => 2,
        (0, Some("haiku")) | (1, Some("luna")) | (2, Some("nano" | "lite")) => 3,
        _ => 4,
    };
    let version = if provider == 0 {
        parts
            .iter()
            .skip(2)
            .filter_map(|p| p.parse::<u32>().ok())
            .collect::<Vec<_>>()
    } else if provider < 3 {
        parts
            .get(1)
            .into_iter()
            .flat_map(|p| p.split('.'))
            .filter_map(|p| p.parse::<u32>().ok())
            .collect()
    } else {
        Vec::new()
    };
    (
        provider,
        Reverse(version.first().copied().unwrap_or(0)),
        tier,
        Reverse(version),
    )
}

fn point(axis: usize, axes: usize, radius: f64) -> (f64, f64) {
    let angle = std::f64::consts::TAU * axis as f64 / axes as f64 - std::f64::consts::FRAC_PI_2;
    (200.0 + radius * angle.cos(), 164.0 + radius * angle.sin())
}

fn polygon(values: impl Iterator<Item = f64>, axes: usize) -> String {
    values
        .enumerate()
        .map(|(i, radius)| {
            let (x, y) = point(i, axes, radius);
            format!("{x:.2},{y:.2}")
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn chart(row: &MatrixRow, kinds: &[String], index: usize) -> String {
    let model = row.model.as_deref().unwrap_or("Unknown model");
    let (provider, provider_name) = provider(row.model.as_deref());
    let axes = kinds.len();
    let peak = row.counts.iter().copied().max().unwrap_or(0);
    let radius = |count: i64| 90.0 * count as f64 / peak.max(1) as f64;
    let share = |count: i64| {
        if row.total > 0 {
            count as f64 / row.total as f64
        } else {
            0.0
        }
    };
    let description = kinds
        .iter()
        .zip(&row.counts)
        .map(|(kind, count)| {
            format!(
                "{kind}: {} requests, {:.1}%",
                fmt_int(*count),
                100.0 * share(*count)
            )
        })
        .collect::<Vec<_>>()
        .join("; ");
    let mut out = format!(
        "<figure class=\"model-radar\" data-provider=\"{provider}\"><figcaption><h3>{}</h3><p>{} requests <span>{provider_name}</span></p></figcaption><svg viewBox=\"0 0 400 330\" role=\"img\" aria-labelledby=\"radar-{index}-title radar-{index}-desc\"><title id=\"radar-{index}-title\">{} task mix</title><desc id=\"radar-{index}-desc\">Share of this model's requests. The outer ring is the largest topic, {:.1}%. {}.</desc>",
        esc(model),
        fmt_int(row.total),
        esc(model),
        share(peak) * 100.0,
        esc(&description)
    );
    for ring in 1..=4 {
        let points = polygon((0..axes).map(|_| 90.0 * ring as f64 / 4.0), axes);
        let _ = write!(out, "<polygon class=\"radar-ring\" points=\"{points}\"/>");
    }
    for axis in 0..axes {
        let (x, y) = point(axis, axes, 90.0);
        let _ = write!(
            out,
            "<line class=\"radar-axis\" x1=\"200\" y1=\"164\" x2=\"{x:.2}\" y2=\"{y:.2}\"/>"
        );
    }
    let points = polygon(row.counts.iter().map(|count| radius(*count)), axes);
    let _ = write!(out, "<polygon class=\"radar-area\" points=\"{points}\"/>");
    // Ring labels sit inside the top axis; task labels sit outside the web.
    for ring in 1..=4 {
        let y = 164.0 - 90.0 * ring as f64 / 4.0;
        let _ = write!(
            out,
            "<text class=\"radar-tick\" x=\"204\" y=\"{y:.2}\">{:.1}%</text>",
            share(peak) * 100.0 * ring as f64 / 4.0
        );
    }
    for (axis, (kind, count)) in kinds.iter().zip(&row.counts).enumerate() {
        let (x, y) = point(axis, axes, 123.0);
        let anchor = if x < 175.0 {
            "end"
        } else if x > 225.0 {
            "start"
        } else {
            "middle"
        };
        let (px, py) = point(axis, axes, radius(*count));
        let _ = write!(
            out,
            "<circle class=\"radar-point\" cx=\"{px:.2}\" cy=\"{py:.2}\" r=\"2.5\"><title>{}: {} requests ({:.1}%)</title></circle><text class=\"radar-label\" x=\"{x:.2}\" y=\"{y:.2}\" text-anchor=\"{anchor}\">{}<tspan class=\"radar-share\" x=\"{x:.2}\" dy=\"15\">{:.1}%</tspan></text>",
            esc(kind),
            fmt_int(*count),
            share(*count) * 100.0,
            esc(&kind.replace('-', " ")),
            share(*count) * 100.0
        );
    }
    out.push_str("</svg></figure>");
    out
}

pub(super) fn render(matrix: &ModelMatrix) -> String {
    let mut out = String::from(
        "<p class=\"radar-note\">Each web scales its largest topic to the outer ring. Ring ticks and axis labels give the share of that model's requests.</p><div class=\"model-radars\">",
    );
    let mut rows = matrix.all_rows.iter().collect::<Vec<_>>();
    rows.sort_by_key(|row| model_order(row.model.as_deref()));
    // Stable sorting keeps request-count order for otherwise identical keys.
    for (i, row) in rows.into_iter().enumerate() {
        out.push_str(&chart(row, &matrix.kinds, i));
    }
    out.push_str("</div>");
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chart_uses_request_shares_and_keeps_exact_counts_accessible() {
        let row = MatrixRow {
            model: Some("claude-test<&".into()),
            models: 1,
            counts: vec![3, 1, 0, 0, 0, 0, 0, 0],
            total: 4,
        };
        let kinds = crate::labels::KINDS
            .iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>();
        let html = chart(&row, &kinds, 7);
        assert!(html.contains("data-provider=\"anthropic\""));
        assert!(html.contains("claude-test&lt;&amp;"));
        assert!(html.contains("research: 3 requests, 75.0%"));
        assert!(html.contains("coding: 1 requests, 25.0%"));
        assert!(html.contains("radar-7-title radar-7-desc"));
        // The largest topic reaches the full radius; other topics retain their ratios.
        assert!(html.contains("radar-area\" points=\"200.00,74.00 221.21,142.79"));
        assert!(!html.contains("Outer ring"));
        assert!(html.contains("largest topic, 75.0%"));
        assert!(html.contains(">18.8%</text>"));
        let empty = chart(
            &MatrixRow {
                total: 0,
                counts: vec![0; 8],
                ..row
            },
            &kinds,
            8,
        );
        assert!(!empty.contains("NaN") && !empty.contains("inf"));
    }

    #[test]
    fn provider_groups_keep_claude_tiers_ahead_of_older_generations() {
        let rows = [
            ("gemini-most", 8),
            ("claude-haiku-4-5", 7),
            ("claude-haiku-5-5", 6),
            ("gpt-medium", 5),
            ("claude-opus-4-6", 4),
            ("claude-opus-5-5", 3),
            ("claude-sonnet-5-5", 2),
            ("claude-fable-5-1", 1),
            ("unknown", 1),
        ]
        .into_iter()
        .map(|(name, total)| MatrixRow {
            model: Some(name.into()),
            models: 1,
            total,
            counts: vec![total, 0, 0, 0, 0, 0, 0, 0],
        })
        .collect();
        let html = render(&ModelMatrix {
            kinds: crate::labels::KINDS.iter().map(|k| k.to_string()).collect(),
            all_rows: rows,
            ..ModelMatrix::default()
        });
        let positions = [
            "claude-fable-5-1",
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-opus-4-6",
            "claude-haiku-4-5",
            "gpt-medium",
            "gemini-most",
            "unknown",
        ]
        .map(|name| html.find(&format!("<h3>{name}</h3>")).unwrap());
        assert!(positions.windows(2).all(|pair| pair[0] < pair[1]));
    }

    #[test]
    fn openai_and_google_follow_tiers_then_older_generations() {
        let mut names = [
            "gpt-5.6-luna",
            "gemini-3.8-flash",
            "gpt-6-luna",
            "gpt-6-sol",
            "gpt-6-astra",
            "gemini-4-argon",
            "gpt-6.1-sol",
            "gpt-5.6-terra",
            "gpt-5.6-sol",
            "gemini-3.1-pro",
            "gpt-5.5",
        ];
        names.sort_by_key(|m| model_order(Some(m)));
        assert_eq!(
            names,
            [
                "gpt-6-astra",
                "gpt-6.1-sol",
                "gpt-6-sol",
                "gpt-6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-5.5",
                "gemini-4-argon",
                "gemini-3.1-pro",
                "gemini-3.8-flash",
            ]
        );
    }

    #[test]
    fn provider_colors_follow_models_even_when_used_in_another_tool() {
        assert_eq!(provider(Some("claude-opus-5-5")).0, "anthropic");
        assert_eq!(provider(Some("gpt-6.1-sol")).0, "openai");
        assert_eq!(provider(Some("gemini-3.8-flash")).0, "google");
        assert_eq!(provider(Some("unrecognized-model")).0, "unknown");
    }
}
