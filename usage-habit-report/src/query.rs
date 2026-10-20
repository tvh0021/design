//! Read-side queries. JSON shapes are documented in README.md.

use anyhow::{Result, bail};
use rusqlite::{Connection, params};
use serde::Serialize;

#[derive(Debug, Default, Clone, Serialize, PartialEq)]
pub struct Tokens {
    pub requests: i64,
    pub input_tokens: i64,
    pub cache_read_input_tokens: i64,
    pub cache_creation_input_tokens: i64,
    pub output_tokens: i64,
    /// Sum of per-request cost estimates; null when no request has a price.
    pub estimated_cost_usd: Option<f64>,
    /// Requests left out of `estimated_cost_usd` because their model has no price.
    pub unpriced_requests: i64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
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
    /// RFC 3339 time the rates behind `estimated_cost_usd` were fetched.
    pub rates_fetched_at: Option<String>,
    pub totals: Tokens,
    pub groups: Vec<SummaryGroup>,
}

/// Totals and per-(tool, model) groups for requests at or after `since_ms`.
pub fn summary(conn: &Connection, since_ms: Option<i64>, now_ms: i64) -> Result<Summary> {
    let mut stmt = conn.prepare(
        r#"SELECT tool, model, COUNT(*),
                  SUM(input_tokens), SUM(cache_read_input_tokens),
                  SUM(cache_creation_input_tokens), SUM(output_tokens),
                  SUM(cost_usd), COUNT(*) - COUNT(cost_usd)
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
                    estimated_cost_usd: r.get(7)?,
                    unpriced_requests: r.get(8)?,
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
        totals.unpriced_requests += g.tokens.unpriced_requests;
        if let Some(c) = g.tokens.estimated_cost_usd {
            *totals.estimated_cost_usd.get_or_insert(0.0) += c;
        }
    }
    Ok(Summary {
        generated_at: rfc3339(now_ms),
        since: since_ms.map(rfc3339),
        rates_fetched_at: None,
        totals,
        groups,
    })
}

/// How a session key matched.
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum MatchedBy {
    SessionId,
    T3ThreadId,
    SessionIdPrefix,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct SessionRef {
    pub tool: String,
    pub session_id: String,
    pub matched_by: MatchedBy,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SessionLookup {
    Found(SessionRef),
    /// More than one session matched at the first stage that matched anything.
    Ambiguous(Vec<SessionRef>),
    NotFound,
}

/// Find a session by key: exact `session_id`, then exact `t3_thread_id`, then
/// `session_id` prefix. The first stage with any match decides.
pub fn resolve_session(conn: &Connection, key: &str) -> Result<SessionLookup> {
    let key = key.trim();
    if key.is_empty() {
        return Ok(SessionLookup::NotFound);
    }
    for (by, cond) in [
        (MatchedBy::SessionId, "session_id = ?1"),
        (MatchedBy::T3ThreadId, "t3_thread_id = ?1"),
        (
            MatchedBy::SessionIdPrefix,
            "substr(session_id, 1, length(?1)) = ?1",
        ),
    ] {
        let mut hits = conn
            .prepare(&format!(
                "SELECT tool, session_id FROM sessions WHERE {cond} ORDER BY tool, session_id"
            ))?
            .query_map(params![key], |r| {
                Ok(SessionRef {
                    tool: r.get(0)?,
                    session_id: r.get(1)?,
                    matched_by: by,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        match hits.len() {
            0 => continue,
            1 => return Ok(SessionLookup::Found(hits.remove(0))),
            _ => return Ok(SessionLookup::Ambiguous(hits)),
        }
    }
    Ok(SessionLookup::NotFound)
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
    let mut rows: Vec<[String; 8]> = vec![[
        "tool".into(),
        "model".into(),
        "requests".into(),
        "input".into(),
        "cache_read".into(),
        "cache_create".into(),
        "output".into(),
        "est. cost".into(),
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
            match t.estimated_cost_usd {
                Some(c) if t.unpriced_requests > 0 => format!("${c:.2}*"),
                Some(c) => format!("${c:.2}"),
                None => "-".into(),
            },
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
    let widths: Vec<usize> = (0..8)
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
    out.push_str(&format!(
        "est. cost: estimated USD at LiteLLM list prices{}",
        match &s.rates_fetched_at {
            Some(t) => format!(" (rates fetched {t})"),
            None => String::new(),
        }
    ));
    if s.totals.unpriced_requests > 0 {
        out.push_str(&format!(
            "; * excludes {} requests whose model has no price (see `query models`)",
            s.totals.unpriced_requests
        ));
    }
    out.push('\n');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_session_stages() {
        let store = crate::store::Store::open_in_memory().unwrap();
        for (tool, sid, t3) in [
            ("codex", "abc-111", Some("t3-thread-1")),
            ("codex", "abc-222", None),
            ("antigravity", "def-333", None),
            ("claude-code", "t3-thread-1x", None),
        ] {
            store
                .conn
                .execute(
                    "INSERT INTO sessions (tool, session_id, t3_thread_id) VALUES (?1, ?2, ?3)",
                    params![tool, sid, t3],
                )
                .unwrap();
        }
        let r = |k| resolve_session(&store.conn, k).unwrap();
        let found = |tool: &str, sid: &str, by| {
            SessionLookup::Found(SessionRef {
                tool: tool.into(),
                session_id: sid.into(),
                matched_by: by,
            })
        };
        assert_eq!(
            r("abc-111"),
            found("codex", "abc-111", MatchedBy::SessionId)
        );
        // A T3 thread id beats a session id that merely starts with it.
        assert_eq!(
            r("t3-thread-1"),
            found("codex", "abc-111", MatchedBy::T3ThreadId)
        );
        assert_eq!(
            r("def"),
            found("antigravity", "def-333", MatchedBy::SessionIdPrefix)
        );
        match r("abc") {
            SessionLookup::Ambiguous(v) => assert_eq!(v.len(), 2),
            other => panic!("{other:?}"),
        }
        assert_eq!(r("zzz"), SessionLookup::NotFound);
        assert_eq!(r("%"), SessionLookup::NotFound);
        assert_eq!(r(""), SessionLookup::NotFound);
    }

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
