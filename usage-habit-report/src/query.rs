//! Read-side queries. JSON shapes are documented in README.md.

use anyhow::{Result, bail};
use rusqlite::{Connection, params};
use serde::Serialize;

#[derive(Debug, Default, Clone, Serialize, PartialEq, Eq)]
pub struct Tokens {
    pub requests: i64,
    pub input_tokens: i64,
    pub cache_read_input_tokens: i64,
    pub cache_creation_input_tokens: i64,
    pub output_tokens: i64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SummaryGroup {
    pub tool: String,
    pub model: Option<String>,
    #[serde(flatten)]
    pub tokens: Tokens,
}

#[derive(Debug, Clone, Serialize)]
pub struct Summary {
    /// RFC 3339 time the summary was computed.
    pub generated_at: String,
    /// RFC 3339 lower bound on request time, or null for all time.
    pub since: Option<String>,
    pub totals: Tokens,
    pub groups: Vec<SummaryGroup>,
}

/// Totals and per-(tool, model) groups for requests at or after `since_ms`.
pub fn summary(conn: &Connection, since_ms: Option<i64>, now_ms: i64) -> Result<Summary> {
    let mut stmt = conn.prepare(
        r#"SELECT tool, model, COUNT(*),
                  SUM(input_tokens), SUM(cache_read_input_tokens),
                  SUM(cache_creation_input_tokens), SUM(output_tokens)
           FROM requests
           WHERE ts_ms >= ?1
           GROUP BY tool, model
           ORDER BY tool, SUM(input_tokens + cache_read_input_tokens
                              + cache_creation_input_tokens + output_tokens) DESC, model"#,
    )?;
    let groups = stmt
        .query_map(params![since_ms.unwrap_or(i64::MIN)], |r| {
            Ok(SummaryGroup {
                tool: r.get(0)?,
                model: r.get(1)?,
                tokens: Tokens {
                    requests: r.get(2)?,
                    input_tokens: r.get(3)?,
                    cache_read_input_tokens: r.get(4)?,
                    cache_creation_input_tokens: r.get(5)?,
                    output_tokens: r.get(6)?,
                },
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut totals = Tokens::default();
    for g in &groups {
        totals.requests += g.tokens.requests;
        totals.input_tokens += g.tokens.input_tokens;
        totals.cache_read_input_tokens += g.tokens.cache_read_input_tokens;
        totals.cache_creation_input_tokens += g.tokens.cache_creation_input_tokens;
        totals.output_tokens += g.tokens.output_tokens;
    }
    Ok(Summary {
        generated_at: rfc3339(now_ms),
        since: since_ms.map(rfc3339),
        totals,
        groups,
    })
}

pub fn rfc3339(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ms)
        .map(|d| d.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
        .unwrap_or_default()
}

/// Parse a relative window such as `12h`, `7d`, `2w` into milliseconds.
pub fn parse_since(s: &str) -> Result<i64> {
    let s = s.trim();
    let Some(unit) = s.chars().last() else {
        bail!("empty --since value");
    };
    let n: i64 = match s[..s.len() - unit.len_utf8()].parse() {
        Ok(n) if n > 0 => n,
        _ => bail!("--since must look like 12h, 7d, or 2w (got {s:?})"),
    };
    let hour = 3_600_000i64;
    let unit_ms = match unit {
        'h' => hour,
        'd' => 24 * hour,
        'w' => 7 * 24 * hour,
        _ => bail!("--since unit must be h, d, or w (got {s:?})"),
    };
    n.checked_mul(unit_ms)
        .ok_or_else(|| anyhow::anyhow!("--since value too large"))
}

/// Plain-text table for the default (non-JSON) output.
pub fn render_summary_table(s: &Summary) -> String {
    let mut rows: Vec<[String; 7]> = vec![[
        "tool".into(),
        "model".into(),
        "requests".into(),
        "input".into(),
        "cache_read".into(),
        "cache_create".into(),
        "output".into(),
    ]];
    let line = |tool: &str, model: &str, t: &Tokens| {
        [
            tool.to_string(),
            model.to_string(),
            t.requests.to_string(),
            t.input_tokens.to_string(),
            t.cache_read_input_tokens.to_string(),
            t.cache_creation_input_tokens.to_string(),
            t.output_tokens.to_string(),
        ]
    };
    for g in &s.groups {
        rows.push(line(
            &g.tool,
            g.model.as_deref().unwrap_or("(none)"),
            &g.tokens,
        ));
    }
    rows.push(line("TOTAL", "", &s.totals));
    let widths: Vec<usize> = (0..7)
        .map(|i| rows.iter().map(|r| r[i].len()).max().unwrap_or(0))
        .collect();
    let mut out = String::new();
    out.push_str(&format!(
        "since: {}\n",
        s.since.as_deref().unwrap_or("all time")
    ));
    for r in rows {
        for (i, cell) in r.iter().enumerate() {
            if i < 2 {
                out.push_str(&format!("{:<w$}  ", cell, w = widths[i]));
            } else {
                out.push_str(&format!("{:>w$}  ", cell, w = widths[i]));
            }
        }
        out.truncate(out.trim_end().len());
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn since_forms() {
        assert_eq!(parse_since("12h").unwrap(), 12 * 3_600_000);
        assert_eq!(parse_since("7d").unwrap(), 7 * 86_400_000);
        assert_eq!(parse_since("2w").unwrap(), 14 * 86_400_000);
        assert!(parse_since("0d").is_err());
        assert!(parse_since("5m").is_err());
        assert!(parse_since("d").is_err());
        assert!(parse_since("").is_err());
    }
}
