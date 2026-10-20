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

impl MatchedBy {
    /// The name used in JSON.
    pub fn as_str(self) -> &'static str {
        match self {
            MatchedBy::SessionId => "session_id",
            MatchedBy::T3ThreadId => "t3_thread_id",
            MatchedBy::SessionIdPrefix => "session_id_prefix",
        }
    }
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

/// Lookup stages in order: exact `session_id`, exact `t3_thread_id`,
/// `session_id` prefix.
const STAGES: [(MatchedBy, &str); 3] = [
    (MatchedBy::SessionId, "session_id = ?1"),
    (MatchedBy::T3ThreadId, "t3_thread_id = ?1"),
    (
        MatchedBy::SessionIdPrefix,
        "substr(session_id, 1, length(?1)) = ?1",
    ),
];

fn lookup_stages(
    conn: &Connection,
    key: &str,
    stages: &[(MatchedBy, &str)],
) -> Result<SessionLookup> {
    let key = key.trim();
    if key.is_empty() {
        return Ok(SessionLookup::NotFound);
    }
    for &(by, cond) in stages {
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

/// Find a session by key: exact `session_id`, then exact `t3_thread_id`, then
/// `session_id` prefix. The first stage with any match decides.
pub fn resolve_session(conn: &Connection, key: &str) -> Result<SessionLookup> {
    lookup_stages(conn, key, &STAGES)
}

/// Find a session by exact T3 thread id only.
pub fn resolve_thread(conn: &Connection, thread_id: &str) -> Result<SessionLookup> {
    lookup_stages(conn, thread_id, &STAGES[1..2])
}

/// A session that a key matched, for the ambiguous response.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SessionCandidate {
    pub tool: String,
    pub session_id: String,
    pub matched_by: MatchedBy,
    pub title: Option<String>,
    pub project: Option<String>,
    pub t3_thread_id: Option<String>,
    /// RFC 3339 UTC time of the session's first record.
    pub started_at: Option<String>,
}

pub fn session_candidates(conn: &Connection, refs: &[SessionRef]) -> Result<Vec<SessionCandidate>> {
    let mut stmt = conn.prepare(
        "SELECT title, project, t3_thread_id, started_at_ms FROM sessions
         WHERE tool = ?1 AND session_id = ?2",
    )?;
    refs.iter()
        .map(|r| {
            let (title, project, t3_thread_id, started): (
                Option<String>,
                Option<String>,
                Option<String>,
                Option<i64>,
            ) = stmt.query_row(params![r.tool, r.session_id], |x| {
                Ok((x.get(0)?, x.get(1)?, x.get(2)?, x.get(3)?))
            })?;
            Ok(SessionCandidate {
                tool: r.tool.clone(),
                session_id: r.session_id.clone(),
                matched_by: r.matched_by,
                title,
                project,
                t3_thread_id,
                started_at: started.map(rfc3339),
            })
        })
        .collect()
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SessionModel {
    /// Raw model name as the harness logged it; null when absent.
    pub model: Option<String>,
    pub canonical_model: Option<String>,
    pub requests: i64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SessionUsage {
    #[serde(flatten)]
    pub tokens: Tokens,
    /// Sum of reasoning tokens where the harness reports them separately;
    /// null when none does.
    pub reasoning_tokens: Option<i64>,
}

/// One request of a session. No prompt or response text.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct RequestRow {
    /// RFC 3339 UTC.
    pub ts: String,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub input_tokens: i64,
    pub cache_read_input_tokens: i64,
    pub cache_creation_input_tokens: i64,
    pub output_tokens: i64,
    pub reasoning_tokens: Option<i64>,
    pub estimated_cost_usd: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SessionReport {
    pub generated_at: String,
    /// RFC 3339 lower bound on request time, or null for all time.
    pub since: Option<String>,
    pub rates_fetched_at: Option<String>,
    pub matched_by: MatchedBy,
    pub tool: String,
    pub session_id: String,
    pub title: Option<String>,
    pub project: Option<String>,
    pub project_name: Option<String>,
    pub is_subagent: bool,
    pub parent_session_id: Option<String>,
    pub agent_type: Option<String>,
    pub t3_thread_id: Option<String>,
    pub t3_title: Option<String>,
    /// Task label: hand-set if present, else the rule result; a subagent
    /// reports its root session's label.
    pub label: Option<String>,
    /// `manual`, `rule`, or null when there is no label.
    pub label_source: Option<String>,
    pub started_at: Option<String>,
    pub ended_at: Option<String>,
    pub elapsed_seconds: Option<i64>,
    /// Models used, most requests first.
    pub models: Vec<SessionModel>,
    pub usage: SessionUsage,
    /// Null for a subagent session.
    pub subagents: Option<crate::agents::SubagentSummary>,
    /// Present only with `--rows`, oldest first.
    pub rows: Option<Vec<RequestRow>>,
}

/// Everything about one session. `since_ms` keeps requests at or after it in
/// `models`, `usage`, `subagents` and `rows`; the session's own start and end
/// are not windowed.
pub fn session_report(
    conn: &Connection,
    found: &SessionRef,
    since_ms: Option<i64>,
    with_rows: bool,
    now_ms: i64,
) -> Result<SessionReport> {
    let (tool, sid) = (found.tool.as_str(), found.session_id.as_str());
    let lo = since_ms.unwrap_or(i64::MIN);
    #[allow(clippy::type_complexity)]
    let row: (
        Option<String>,
        Option<String>,
        Option<String>,
        bool,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<i64>,
        Option<i64>,
    ) = conn.query_row(
        "SELECT title, project, project_name, is_subagent, parent_session_id, agent_type,
                t3_thread_id, t3_title, started_at_ms, ended_at_ms
         FROM sessions WHERE tool = ?1 AND session_id = ?2",
        params![tool, sid],
        |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get::<_, i64>(3)? != 0,
                r.get(4)?,
                r.get(5)?,
                r.get(6)?,
                r.get(7)?,
                r.get(8)?,
                r.get(9)?,
            ))
        },
    )?;
    let (title, project, project_name, is_subagent, parent, agent_type) =
        (row.0, row.1, row.2, row.3, row.4, row.5);
    let (t3_thread_id, t3_title, started_ms, ended_ms) = (row.6, row.7, row.8, row.9);

    let (label, label_source) = session_label(conn, tool, sid)?;

    let (usage, first_ms, last_ms) = conn.query_row(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(cache_read_input_tokens), 0),
                COALESCE(SUM(cache_creation_input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                SUM(cost_usd), COUNT(*) - COUNT(cost_usd), SUM(reasoning_tokens),
                MIN(ts_ms), MAX(ts_ms)
         FROM requests WHERE tool = ?1 AND session_id = ?2 AND ts_ms >= ?3",
        params![tool, sid, lo],
        |r| {
            Ok((
                SessionUsage {
                    tokens: Tokens {
                        requests: r.get(0)?,
                        input_tokens: r.get(1)?,
                        cache_read_input_tokens: r.get(2)?,
                        cache_creation_input_tokens: r.get(3)?,
                        output_tokens: r.get(4)?,
                        estimated_cost_usd: r.get(5)?,
                        unpriced_requests: r.get(6)?,
                    },
                    reasoning_tokens: r.get(7)?,
                },
                r.get::<_, Option<i64>>(8)?,
                r.get::<_, Option<i64>>(9)?,
            ))
        },
    )?;
    let models = conn
        .prepare(
            "SELECT model, MAX(canonical_model), COUNT(*) FROM requests
             WHERE tool = ?1 AND session_id = ?2 AND ts_ms >= ?3
             GROUP BY model ORDER BY COUNT(*) DESC, model IS NULL, model",
        )?
        .query_map(params![tool, sid, lo], |r| {
            Ok(SessionModel {
                model: r.get(0)?,
                canonical_model: r.get(1)?,
                requests: r.get(2)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let rows = if with_rows {
        Some(
            conn.prepare(
                "SELECT ts_ms, model, effort, input_tokens, cache_read_input_tokens,
                        cache_creation_input_tokens, output_tokens, reasoning_tokens, cost_usd
                 FROM requests WHERE tool = ?1 AND session_id = ?2 AND ts_ms >= ?3
                 ORDER BY ts_ms, id",
            )?
            .query_map(params![tool, sid, lo], |r| {
                Ok(RequestRow {
                    ts: rfc3339(r.get(0)?),
                    model: r.get(1)?,
                    effort: r.get(2)?,
                    input_tokens: r.get(3)?,
                    cache_read_input_tokens: r.get(4)?,
                    cache_creation_input_tokens: r.get(5)?,
                    output_tokens: r.get(6)?,
                    reasoning_tokens: r.get(7)?,
                    estimated_cost_usd: r.get(8)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?,
        )
    } else {
        None
    };
    let subagents = if is_subagent {
        None
    } else {
        Some(crate::agents::session_subagents(conn, tool, sid, since_ms)?)
    };
    // The session's own span, falling back to its requests.
    let started = started_ms.or(first_ms);
    let ended = ended_ms.or(last_ms);
    Ok(SessionReport {
        generated_at: rfc3339(now_ms),
        since: since_ms.map(rfc3339),
        rates_fetched_at: None,
        matched_by: found.matched_by,
        tool: tool.to_string(),
        session_id: sid.to_string(),
        title,
        project,
        project_name,
        is_subagent,
        parent_session_id: parent,
        agent_type,
        t3_thread_id,
        t3_title,
        label,
        label_source,
        started_at: started.map(rfc3339),
        ended_at: ended.map(rfc3339),
        elapsed_seconds: started.zip(ended).map(|(s, e)| ((e - s) / 1000).max(0)),
        models,
        usage,
        subagents,
        rows,
    })
}

/// Label and where it came from. A subagent takes its root session's label.
fn session_label(
    conn: &Connection,
    tool: &str,
    session_id: &str,
) -> Result<(Option<String>, Option<String>)> {
    let mut at = session_id.to_string();
    for _ in 0..32 {
        let (is_sub, parent, rule, rule_src): (
            i64,
            Option<String>,
            Option<String>,
            Option<String>,
        ) = match conn.query_row(
            "SELECT is_subagent, parent_session_id, label, label_source FROM sessions
                 WHERE tool = ?1 AND session_id = ?2",
            params![tool, at],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        ) {
            Ok(v) => v,
            Err(rusqlite::Error::QueryReturnedNoRows) => break,
            Err(e) => return Err(e.into()),
        };
        if is_sub != 0 {
            match parent {
                Some(p) => {
                    at = p;
                    continue;
                }
                None => break,
            }
        }
        let manual: Option<String> = conn
            .query_row(
                "SELECT label FROM session_labels WHERE tool = ?1 AND session_id = ?2",
                params![tool, at],
                |r| r.get(0),
            )
            .ok();
        return Ok(match (manual, rule) {
            (Some(m), _) => (Some(m), Some("manual".into())),
            (None, Some(r)) => (Some(r), Some(rule_src.unwrap_or_else(|| "rule".into()))),
            (None, None) => (None, None),
        });
    }
    Ok((None, None))
}

fn fmt_cost(t: &Tokens) -> String {
    match t.estimated_cost_usd {
        Some(c) if t.unpriced_requests > 0 => format!("${c:.2}*"),
        Some(c) => format!("${c:.2}"),
        None => "-".into(),
    }
}

fn fmt_elapsed(secs: i64) -> String {
    let (h, m, s) = (secs / 3600, secs / 60 % 60, secs % 60);
    if h > 0 {
        format!("{h}h {m:02}m")
    } else if m > 0 {
        format!("{m}m {s:02}s")
    } else {
        format!("{s}s")
    }
}

/// Plain-text rendering for the default (non-JSON) output.
pub fn render_session(r: &SessionReport) -> String {
    let dash = |o: &Option<String>| o.clone().unwrap_or_else(|| "-".into());
    let mut out = String::new();
    let mut line = |k: &str, v: String| out.push_str(&format!("{k:<12}{v}\n"));
    line("tool", r.tool.clone());
    line(
        "session",
        format!(
            "{}{}",
            r.session_id,
            if r.is_subagent { " (subagent)" } else { "" }
        ),
    );
    line("title", dash(&r.title));
    line("project", dash(&r.project));
    if let Some(p) = &r.parent_session_id {
        line("parent", p.clone());
    }
    if r.t3_thread_id.is_some() || r.t3_title.is_some() {
        line(
            "t3 thread",
            format!("{} ({})", dash(&r.t3_thread_id), dash(&r.t3_title)),
        );
    }
    line(
        "label",
        match (&r.label, &r.label_source) {
            (Some(l), Some(s)) => format!("{l} ({s})"),
            _ => "-".into(),
        },
    );
    line("started", dash(&r.started_at));
    line("ended", dash(&r.ended_at));
    line("elapsed", r.elapsed_seconds.map_or("-".into(), fmt_elapsed));
    if let Some(s) = &r.since {
        line("since", s.clone());
    }
    let t = &r.usage.tokens;
    line("requests", t.requests.to_string());
    line(
        "tokens",
        format!(
            "input {}, cache_read {}, cache_create {}, output {}, reasoning {}",
            t.input_tokens,
            t.cache_read_input_tokens,
            t.cache_creation_input_tokens,
            t.output_tokens,
            r.usage
                .reasoning_tokens
                .map_or("-".into(), |n| n.to_string())
        ),
    );
    line(
        "est. cost",
        format!(
            "{}{}",
            fmt_cost(t),
            if t.unpriced_requests > 0 {
                format!(" (excludes {} requests with no price)", t.unpriced_requests)
            } else {
                String::new()
            }
        ),
    );
    let models = r
        .models
        .iter()
        .map(|m| format!("{} x{}", m.model.as_deref().unwrap_or("(none)"), m.requests))
        .collect::<Vec<_>>()
        .join(", ");
    line(
        "models",
        if models.is_empty() {
            "-".into()
        } else {
            models
        },
    );
    if let Some(s) = &r.subagents {
        let share = s
            .subagent_token_share
            .map_or("-".into(), |v| format!("{:.1}%", v * 100.0));
        let sm = s
            .subagent_models
            .iter()
            .map(|m| format!("{} x{}", m.model.as_deref().unwrap_or("(none)"), m.requests))
            .collect::<Vec<_>>()
            .join(", ");
        line(
            "subagents",
            format!(
                "{} (max fan-out {}, token share {}){}",
                s.subagent_count,
                s.max_concurrent_fanout,
                share,
                if sm.is_empty() {
                    String::new()
                } else {
                    format!("; models: {sm}")
                }
            ),
        );
    }
    if let Some(rows) = &r.rows {
        out.push('\n');
        let mut tab = vec![
            [
                "ts",
                "model",
                "effort",
                "input",
                "cache_read",
                "cache_create",
                "output",
                "reasoning",
                "est. cost",
            ]
            .map(String::from)
            .to_vec(),
        ];
        for x in rows {
            tab.push(vec![
                x.ts.clone(),
                x.model.clone().unwrap_or_else(|| "(none)".into()),
                x.effort.clone().unwrap_or_else(|| "-".into()),
                x.input_tokens.to_string(),
                x.cache_read_input_tokens.to_string(),
                x.cache_creation_input_tokens.to_string(),
                x.output_tokens.to_string(),
                x.reasoning_tokens.map_or("-".into(), |n| n.to_string()),
                x.estimated_cost_usd
                    .map_or("-".into(), |c| format!("${c:.4}")),
            ]);
        }
        out.push_str(&crate::project::table(&tab, 3));
    }
    out
}

/// Candidate list for an ambiguous key.
pub fn render_session_candidates(key: &str, c: &[SessionCandidate]) -> String {
    let mut out = format!("ambiguous: {} sessions match {key:?}\n", c.len());
    let mut tab = vec![
        [
            "tool",
            "session",
            "matched by",
            "started",
            "project",
            "title",
        ]
        .map(String::from)
        .to_vec(),
    ];
    for x in c {
        tab.push(vec![
            x.tool.clone(),
            x.session_id.clone(),
            x.matched_by.as_str().to_string(),
            x.started_at.clone().unwrap_or_else(|| "-".into()),
            x.project.clone().unwrap_or_else(|| "-".into()),
            x.title.clone().unwrap_or_else(|| "-".into()),
        ]);
    }
    out.push_str(&crate::project::table(&tab, 6));
    out
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
