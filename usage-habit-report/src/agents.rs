//! Multi-agent metrics (ticket 06), computed at query time from the columns
//! the ingesters already fill: `sessions.is_subagent` / `parent_session_id`
//! and `requests.ts_ms` / `model` / `effort`.
//!
//! A subagent is attached to its root: the nearest ancestor that is not a
//! subagent (Codex can spawn subagents from subagents). Tokens here are
//! `input_tokens + output_tokens` (uncached input plus output); cache reads and
//! writes are left out so that the share is not dominated by prompt caching.

use crate::query::rfc3339;
use anyhow::Result;
use rusqlite::{Connection, params};
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};

const HEAVIEST: usize = 10;

/// Whether a tool's logs link subagents to their parent, and what to know.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Coverage {
    pub supported: bool,
    pub note: String,
}

pub fn coverage() -> BTreeMap<String, Coverage> {
    let c = |supported: bool, note: &str| Coverage {
        supported,
        note: note.to_string(),
    };
    BTreeMap::from([
        (
            "claude-code".to_string(),
            c(
                true,
                "Subagent transcripts sit under their parent session's folder, so every subagent is linked. Agent type comes from the .meta.json file; reasoning effort is recorded when the record carries it.",
            ),
        ),
        (
            "codex".to_string(),
            c(
                true,
                "Links thread_spawn subagents through session_meta. Internal guardian sessions are excluded by design. A few subagent sessions carry no parent id; they are counted under unlinked_subagent_sessions and left out of per-session metrics. Reasoning effort is recorded per turn.",
            ),
        ),
        (
            "antigravity".to_string(),
            c(
                true,
                "Child conversations are linked to their parent through the conversation's lineage fields. Reasoning effort is recorded only when the selected-model string carries it.",
            ),
        ),
    ])
}

/// Largest number of intervals that overlap at any instant. Intervals are
/// closed, so two that touch at one instant overlap there.
pub fn max_concurrent(intervals: &[(i64, i64)]) -> usize {
    // At equal times a start (+1) sorts before an end (-1).
    let mut events: Vec<(i64, i32)> = intervals
        .iter()
        .flat_map(|&(s, e)| [(s, 1), (e.max(s), -1)])
        .collect();
    events.sort_by(|a, b| a.0.cmp(&b.0).then(b.1.cmp(&a.1)));
    let (mut now, mut best) = (0i32, 0i32);
    for (_, d) in events {
        now += d;
        best = best.max(now);
    }
    best as usize
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelCount {
    pub model: Option<String>,
    pub requests: i64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct EffortCount {
    /// Null counts requests whose log has no effort.
    pub effort: Option<String>,
    pub requests: i64,
}

/// Multi-agent shape of one top-level session.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SessionAgents {
    pub tool: String,
    pub session_id: String,
    pub title: Option<String>,
    pub subagent_count: usize,
    pub subagent_models: Vec<ModelCount>,
    pub subagent_efforts: Vec<EffortCount>,
    pub subagent_tokens: i64,
    pub total_tokens: i64,
    /// `subagent_tokens / total_tokens`; null when the session has no tokens.
    pub subagent_token_share: Option<f64>,
    pub max_concurrent_fanout: usize,
    pub display_title: Option<String>,
    pub title_source: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct HistogramBucket {
    /// `0`, `1`, `2`, `3-4`, `5-9`, `10-19`, `20+`
    pub bucket: String,
    pub min: usize,
    /// Null for the open-ended last bucket.
    pub max: Option<usize>,
    pub sessions: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ToolAgents {
    pub tool: String,
    pub top_level_sessions: usize,
    pub sessions_with_subagents: usize,
    pub subagent_sessions: usize,
    /// Subagent sessions whose parent chain does not reach a top-level session.
    pub unlinked_subagent_sessions: usize,
    pub cross_tool_links: usize,
    pub timing_matched_links: usize,
    pub parents_with_imported_children: usize,
    pub subagent_tokens: i64,
    pub total_tokens: i64,
    pub subagent_token_share: Option<f64>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelMixEntry {
    pub model: Option<String>,
    pub requests: i64,
    pub tokens: i64,
    /// Fraction of this side's requests.
    pub request_share: f64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelMix {
    pub main_thread: Vec<ModelMixEntry>,
    pub subagent: Vec<ModelMixEntry>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct TokenShare {
    pub subagent_tokens: i64,
    pub total_tokens: i64,
    pub share: Option<f64>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Totals {
    pub top_level_sessions: usize,
    pub sessions_with_subagents: usize,
    pub subagent_sessions: usize,
    pub unlinked_subagent_sessions: usize,
    pub cross_tool_links: usize,
    pub timing_matched_links: usize,
    pub parents_with_imported_children: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct AgentsReport {
    pub generated_at: String,
    pub since: Option<String>,
    pub coverage: BTreeMap<String, Coverage>,
    pub totals: Totals,
    pub subagent_token_share: TokenShare,
    pub histogram: Vec<HistogramBucket>,
    pub by_tool: Vec<ToolAgents>,
    pub model_mix: ModelMix,
    pub subagent_efforts: Vec<EffortCount>,
    pub heaviest_fanouts: Vec<SessionAgents>,
}

/// (model, effort, requests) of one session.
type ModelEffortCount = (Option<String>, Option<String>, i64);

struct SessionRow {
    tool: String,
    id: String,
    is_subagent: bool,
    parent: Option<String>,
    parent_tool: String,
    title: Option<String>,
    display_title: Option<String>,
    title_source: Option<String>,
}

#[derive(Default, Clone, Copy)]
struct Span {
    first: i64,
    last: i64,
    tokens: i64,
}

const BUCKETS: &[(usize, Option<usize>)] = &[
    (0, Some(0)),
    (1, Some(1)),
    (2, Some(2)),
    (3, Some(4)),
    (5, Some(9)),
    (10, Some(19)),
    (20, None),
];

fn bucket_name(min: usize, max: Option<usize>) -> String {
    match max {
        Some(m) if m == min => min.to_string(),
        Some(m) => format!("{min}-{m}"),
        None => format!("{min}+"),
    }
}

fn new_tool(tool: &str) -> ToolAgents {
    ToolAgents {
        tool: tool.to_string(),
        top_level_sessions: 0,
        sessions_with_subagents: 0,
        subagent_sessions: 0,
        unlinked_subagent_sessions: 0,
        cross_tool_links: 0,
        timing_matched_links: 0,
        parents_with_imported_children: 0,
        subagent_tokens: 0,
        total_tokens: 0,
        subagent_token_share: None,
    }
}

fn share(part: i64, whole: i64) -> Option<f64> {
    (whole > 0).then(|| part as f64 / whole as f64)
}

/// Multi-agent metrics over requests at or after `since_ms`. With a `since`,
/// only sessions that have a request in the window are counted.
pub fn agents_report(
    conn: &Connection,
    since_ms: Option<i64>,
    now_ms: i64,
) -> Result<AgentsReport> {
    agents_report_for(conn, since_ms, None, now_ms)
}

/// Restrict root-tree metrics to one parent tool. Descendants may use another
/// tool. Request-wide token shares and model mixes retain the request tool filter.
pub fn agents_report_for(
    conn: &Connection,
    since_ms: Option<i64>,
    tool: Option<&str>,
    now_ms: i64,
) -> Result<AgentsReport> {
    let lo = since_ms.unwrap_or(i64::MIN);

    let sessions = conn
        .prepare(
            "SELECT tool, session_id, is_subagent, parent_session_id, title, COALESCE(parent_tool, tool), display_title, title_source FROM sessions",
        )?
        .query_map([], |r| {
            Ok(SessionRow {
                tool: r.get(0)?,
                id: r.get(1)?,
                is_subagent: r.get::<_, i64>(2)? != 0,
                parent: r.get(3)?,
                parent_tool: r.get(5)?,
                title: r.get(4)?,
                display_title: r.get(6)?,
                title_source: r.get(7)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let index: HashMap<(&str, &str), usize> = sessions
        .iter()
        .enumerate()
        .map(|(i, s)| ((s.tool.as_str(), s.id.as_str()), i))
        .collect();

    // Per-session request span and tokens inside the window.
    let spans: HashMap<(String, String), Span> = conn
        .prepare(
            "SELECT tool, session_id, MIN(ts_ms), MAX(ts_ms), SUM(input_tokens + output_tokens)
             FROM requests WHERE ts_ms >= ?1
             GROUP BY tool, session_id",
        )?
        .query_map(params![lo], |r| {
            Ok((
                (r.get(0)?, r.get(1)?),
                Span {
                    first: r.get(2)?,
                    last: r.get(3)?,
                    tokens: r.get(4)?,
                },
            ))
        })?
        .collect::<rusqlite::Result<_>>()?;
    // Per-session model and effort counts inside the window.
    let mut mix: HashMap<(String, String), Vec<ModelEffortCount>> = HashMap::new();
    {
        let mut stmt = conn.prepare(
            "SELECT tool, session_id, COALESCE(canonical_model, model) AS model, effort, COUNT(*)
             FROM requests
             WHERE ts_ms >= ?1
             GROUP BY tool, session_id, 3, effort",
        )?;
        let mut rows = stmt.query(params![lo])?;
        while let Some(r) = rows.next()? {
            mix.entry((r.get(0)?, r.get(1)?))
                .or_default()
                .push((r.get(2)?, r.get(3)?, r.get(4)?));
        }
    }

    // Root of each subagent: follow parents to the first non-subagent session.
    let root_of = |start: usize| -> Option<usize> {
        let mut at = start;
        for _ in 0..32 {
            let s = &sessions[at];
            if !s.is_subagent {
                return Some(at);
            }
            at = *index.get(&(s.parent_tool.as_str(), s.parent.as_deref()?))?;
        }
        None
    };

    #[derive(Default)]
    struct Tree {
        subagents: usize,
        intervals: Vec<(i64, i64)>,
        sub_tokens: i64,
        own_tokens: i64,
        models: BTreeMap<Option<String>, i64>,
        efforts: BTreeMap<Option<String>, i64>,
        live: bool,
    }
    let mut trees: HashMap<usize, Tree> = HashMap::new();
    let mut unlinked: BTreeMap<&str, usize> = BTreeMap::new();
    let mut sub_total: BTreeMap<&str, usize> = BTreeMap::new();
    let mut all_efforts: BTreeMap<Option<String>, i64> = BTreeMap::new();
    for (i, s) in sessions.iter().enumerate() {
        let key = (s.tool.clone(), s.id.clone());
        let span = spans.get(&key);
        // With a window, a top-level session counts only if it or one of its
        // subagents has a request in it (`live`); a subagent only if it does.
        if !s.is_subagent {
            if tool.is_some_and(|tool| tool != s.tool) {
                continue;
            }
            let t = trees.entry(i).or_default();
            t.own_tokens += span.map_or(0, |x| x.tokens);
            t.live |= span.is_some();
            continue;
        }
        if since_ms.is_some() && span.is_none() {
            continue;
        }
        let selected = tool.is_none_or(|tool| tool == s.tool);
        if selected {
            *sub_total.entry(s.tool.as_str()).or_default() += 1;
            for (_, effort, n) in mix.get(&key).into_iter().flatten() {
                *all_efforts.entry(effort.clone()).or_default() += n;
            }
        }
        let Some(root) = root_of(i) else {
            if selected {
                *unlinked.entry(s.tool.as_str()).or_default() += 1;
            }
            continue;
        };
        let t = trees.entry(root).or_default();
        t.subagents += 1;
        t.live |= span.is_some();
        if let Some(x) = span {
            t.intervals.push((x.first, x.last));
            t.sub_tokens += x.tokens;
        }
        for (model, effort, n) in mix.get(&key).into_iter().flatten() {
            *t.models.entry(model.clone()).or_default() += n;
            *t.efforts.entry(effort.clone()).or_default() += n;
        }
    }

    let mut per_session: Vec<SessionAgents> = Vec::new();
    let mut by_tool: BTreeMap<&str, ToolAgents> = BTreeMap::new();
    let mut hist = vec![0usize; BUCKETS.len()];
    for (&i, t) in &trees {
        if since_ms.is_some() && !t.live {
            continue;
        }
        let s = &sessions[i];
        if tool.is_some_and(|tool| tool != s.tool) {
            continue;
        }
        let tool = by_tool
            .entry(s.tool.as_str())
            .or_insert_with(|| new_tool(&s.tool));
        tool.top_level_sessions += 1;
        tool.sessions_with_subagents += (t.subagents > 0) as usize;
        tool.subagent_sessions += t.subagents;
        tool.subagent_tokens += t.sub_tokens;
        tool.total_tokens += t.own_tokens + t.sub_tokens;
        let b = BUCKETS
            .iter()
            .position(|&(min, max)| t.subagents >= min && max.is_none_or(|m| t.subagents <= m))
            .unwrap_or(0);
        hist[b] += 1;
        if t.subagents > 0 {
            let total = t.own_tokens + t.sub_tokens;
            per_session.push(SessionAgents {
                tool: s.tool.clone(),
                session_id: s.id.clone(),
                title: s.title.clone(),
                display_title: s.display_title.clone(),
                title_source: s.title_source.clone(),
                subagent_count: t.subagents,
                subagent_models: sorted_counts(&t.models)
                    .into_iter()
                    .map(|(model, requests)| ModelCount { model, requests })
                    .collect(),
                subagent_efforts: sorted_counts(&t.efforts)
                    .into_iter()
                    .map(|(effort, requests)| EffortCount { effort, requests })
                    .collect(),
                subagent_tokens: t.sub_tokens,
                total_tokens: total,
                subagent_token_share: share(t.sub_tokens, total),
                max_concurrent_fanout: max_concurrent(&t.intervals),
            });
        }
    }
    for (tool, n) in &unlinked {
        by_tool
            .entry(tool)
            .or_insert_with(|| new_tool(tool))
            .unlinked_subagent_sessions = *n;
    }
    let mut by_tool: Vec<ToolAgents> = by_tool.into_values().collect();
    for t in &mut by_tool {
        t.subagent_token_share = share(t.subagent_tokens, t.total_tokens);
    }

    per_session.sort_by(|a, b| {
        b.max_concurrent_fanout
            .cmp(&a.max_concurrent_fanout)
            .then(b.subagent_count.cmp(&a.subagent_count))
            .then(b.subagent_tokens.cmp(&a.subagent_tokens))
            .then_with(|| (&a.tool, &a.session_id).cmp(&(&b.tool, &b.session_id)))
    });
    per_session.truncate(HEAVIEST);

    // Global figures come straight from `requests.is_subagent`, so subagents
    // with no known parent still count.
    let mut main_mix: BTreeMap<Option<String>, (i64, i64)> = BTreeMap::new();
    let mut sub_mix: BTreeMap<Option<String>, (i64, i64)> = BTreeMap::new();
    let (mut sub_tokens, mut total_tokens) = (0i64, 0i64);
    {
        let mut stmt = conn.prepare(
            "SELECT is_subagent, COALESCE(canonical_model, model) AS model, COUNT(*),
                    SUM(input_tokens + output_tokens)
             FROM requests WHERE ts_ms >= ?1 AND (?2 IS NULL OR tool = ?2)
             GROUP BY is_subagent, 2",
        )?;
        let mut rows = stmt.query(params![lo, tool])?;
        while let Some(r) = rows.next()? {
            let is_sub = r.get::<_, i64>(0)? != 0;
            let (n, tok): (i64, i64) = (r.get(2)?, r.get(3)?);
            total_tokens += tok;
            let side = if is_sub {
                sub_tokens += tok;
                &mut sub_mix
            } else {
                &mut main_mix
            };
            let e = side.entry(r.get(1)?).or_default();
            e.0 += n;
            e.1 += tok;
        }
    }

    let link_counts = |only: Option<&str>| -> Result<(usize, usize, usize)> {
        Ok(conn.query_row("SELECT COALESCE(SUM(COALESCE(parent_tool, tool) <> tool), 0),
            COALESCE(SUM(lineage_source = 'timing'), 0), COUNT(DISTINCT COALESCE(parent_tool, tool) || ':' || parent_session_id)
            FROM sessions WHERE lineage_source IN ('t3', 'timing')
            AND (?1 IS NULL OR COALESCE(parent_tool, tool) = ?1)
            AND (?2 IS NULL OR EXISTS (SELECT 1 FROM requests r WHERE r.tool = sessions.tool AND r.session_id = sessions.session_id AND r.ts_ms >= ?2))",
            params![only, since_ms], |r| Ok((r.get::<_, i64>(0)? as usize, r.get::<_, i64>(1)? as usize, r.get::<_, i64>(2)? as usize)))?)
    };
    for t in &mut by_tool {
        (
            t.cross_tool_links,
            t.timing_matched_links,
            t.parents_with_imported_children,
        ) = link_counts(Some(&t.tool))?;
    }
    let (cross_tool_links, timing_matched_links, parents_with_imported_children) =
        link_counts(tool)?;
    let totals = Totals {
        cross_tool_links,
        timing_matched_links,
        parents_with_imported_children,
        top_level_sessions: by_tool.iter().map(|t| t.top_level_sessions).sum(),
        sessions_with_subagents: by_tool.iter().map(|t| t.sessions_with_subagents).sum(),
        subagent_sessions: sub_total.values().sum(),
        unlinked_subagent_sessions: unlinked.values().sum(),
    };
    Ok(AgentsReport {
        generated_at: rfc3339(now_ms),
        since: since_ms.map(rfc3339),
        coverage: coverage(),
        totals,
        subagent_token_share: TokenShare {
            subagent_tokens: sub_tokens,
            total_tokens,
            share: share(sub_tokens, total_tokens),
        },
        histogram: BUCKETS
            .iter()
            .zip(hist)
            .map(|(&(min, max), sessions)| HistogramBucket {
                bucket: bucket_name(min, max),
                min,
                max,
                sessions,
            })
            .collect(),
        by_tool,
        model_mix: ModelMix {
            main_thread: mix_entries(main_mix),
            subagent: mix_entries(sub_mix),
        },
        subagent_efforts: sorted_counts(&all_efforts)
            .into_iter()
            .map(|(effort, requests)| EffortCount { effort, requests })
            .collect(),
        heaviest_fanouts: per_session,
    })
}

/// Subagent summary of one top-level session (same figures and definitions as
/// [`SessionAgents`], without the identity fields).
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SubagentSummary {
    pub subagent_count: usize,
    pub subagent_models: Vec<ModelCount>,
    pub subagent_efforts: Vec<EffortCount>,
    /// Uncached input plus output tokens of all subagents.
    pub subagent_tokens: i64,
    /// The same measure over the session and its subagents.
    pub total_tokens: i64,
    pub subagent_token_share: Option<f64>,
    pub max_concurrent_fanout: usize,
}

/// Subagent summary for one session: every subagent below it (nested ones
/// included), counting requests at or after `since_ms`. With a window, a
/// subagent counts only if it has a request in it, as in [`agents_report`].
pub fn session_subagents(
    conn: &Connection,
    tool: &str,
    session_id: &str,
    since_ms: Option<i64>,
) -> Result<SubagentSummary> {
    let lo = since_ms.unwrap_or(i64::MIN);
    let descendants = conn
        .prepare(
            "WITH RECURSIVE tree(tool, sid) AS (
            SELECT ?1, ?2 UNION SELECT s.tool, s.session_id FROM sessions s JOIN tree t
            ON COALESCE(s.parent_tool, s.tool) = t.tool AND s.parent_session_id = t.sid
         ) SELECT tool, sid FROM tree WHERE tool <> ?1 OR sid <> ?2",
        )?
        .query_map(params![tool, session_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;

    let own_tokens: i64 = conn.query_row(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM requests
         WHERE tool = ?1 AND session_id = ?2 AND ts_ms >= ?3",
        params![tool, session_id, lo],
        |r| r.get(0),
    )?;
    let mut span_stmt = conn.prepare(
        "SELECT MIN(ts_ms), MAX(ts_ms), COALESCE(SUM(input_tokens + output_tokens), 0)
         FROM requests WHERE tool = ?1 AND session_id = ?2 AND ts_ms >= ?3",
    )?;
    let mut mix_stmt = conn.prepare(
        "SELECT COALESCE(canonical_model, model) AS model, effort, COUNT(*) FROM requests
         WHERE tool = ?1 AND session_id = ?2 AND ts_ms >= ?3 GROUP BY 1, effort",
    )?;
    let mut count = 0usize;
    let mut intervals = Vec::new();
    let mut sub_tokens = 0i64;
    let mut models: BTreeMap<Option<String>, i64> = BTreeMap::new();
    let mut efforts: BTreeMap<Option<String>, i64> = BTreeMap::new();
    for (child_tool, d) in &descendants {
        let (first, last, tokens): (Option<i64>, Option<i64>, i64) = span_stmt
            .query_row(params![child_tool, d, lo], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })?;
        if since_ms.is_some() && first.is_none() {
            continue;
        }
        count += 1;
        if let (Some(f), Some(l)) = (first, last) {
            intervals.push((f, l));
            sub_tokens += tokens;
        }
        let mut rows = mix_stmt.query(params![child_tool, d, lo])?;
        while let Some(r) = rows.next()? {
            let n: i64 = r.get(2)?;
            *models.entry(r.get(0)?).or_default() += n;
            *efforts.entry(r.get(1)?).or_default() += n;
        }
    }
    let total = own_tokens + sub_tokens;
    Ok(SubagentSummary {
        subagent_count: count,
        subagent_models: sorted_counts(&models)
            .into_iter()
            .map(|(model, requests)| ModelCount { model, requests })
            .collect(),
        subagent_efforts: sorted_counts(&efforts)
            .into_iter()
            .map(|(effort, requests)| EffortCount { effort, requests })
            .collect(),
        subagent_tokens: sub_tokens,
        total_tokens: total,
        subagent_token_share: share(sub_tokens, total),
        max_concurrent_fanout: max_concurrent(&intervals),
    })
}

/// Entries ordered by count descending, then name.
fn sorted_counts(m: &BTreeMap<Option<String>, i64>) -> Vec<(Option<String>, i64)> {
    let mut v: Vec<_> = m.iter().map(|(k, n)| (k.clone(), *n)).collect();
    v.sort_by(|a, b| {
        b.1.cmp(&a.1)
            .then_with(|| (a.0.is_none(), &a.0).cmp(&(b.0.is_none(), &b.0)))
    });
    v
}

fn mix_entries(m: BTreeMap<Option<String>, (i64, i64)>) -> Vec<ModelMixEntry> {
    let total: i64 = m.values().map(|v| v.0).sum();
    let mut v: Vec<_> = m
        .into_iter()
        .map(|(model, (requests, tokens))| ModelMixEntry {
            model,
            requests,
            tokens,
            request_share: if total > 0 {
                requests as f64 / total as f64
            } else {
                0.0
            },
        })
        .collect();
    v.sort_by(|a, b| {
        b.requests
            .cmp(&a.requests)
            .then_with(|| (a.model.is_none(), &a.model).cmp(&(b.model.is_none(), &b.model)))
    });
    v
}

fn pct(x: Option<f64>) -> String {
    x.map_or("-".into(), |v| format!("{:.1}%", v * 100.0))
}

/// Plain-text rendering for the default (non-JSON) output.
pub fn render_agents(r: &AgentsReport) -> String {
    let mut out = format!("since: {}\n", r.since.as_deref().unwrap_or("all time"));
    out.push_str(&format!(
        "top-level sessions: {}, with subagents: {}, subagent sessions: {} ({} with no known parent)\n",
        r.totals.top_level_sessions,
        r.totals.sessions_with_subagents,
        r.totals.subagent_sessions,
        r.totals.unlinked_subagent_sessions
    ));
    out.push_str(&format!(
        "subagent token share (input + output): {} ({} of {})\n",
        pct(r.subagent_token_share.share),
        r.subagent_token_share.subagent_tokens,
        r.subagent_token_share.total_tokens
    ));
    out.push_str(&format!(
        "cross-tool links: {}, timing-matched links: {}\n",
        r.totals.cross_tool_links, r.totals.timing_matched_links
    ));
    out.push_str("\nsubagents per session:\n");
    for b in &r.histogram {
        out.push_str(&format!("  {:>6}  {}\n", b.bucket, b.sessions));
    }
    out.push_str("\nby tool:\n");
    for t in &r.by_tool {
        out.push_str(&format!(
            "  {}: {} sessions, {} with subagents, {} subagents, {} unlinked, token share {}\n",
            t.tool,
            t.top_level_sessions,
            t.sessions_with_subagents,
            t.subagent_sessions,
            t.unlinked_subagent_sessions,
            pct(t.subagent_token_share)
        ));
    }
    for (name, side) in [
        ("main thread", &r.model_mix.main_thread),
        ("subagent", &r.model_mix.subagent),
    ] {
        out.push_str(&format!("\nmodel mix, {name} (requests):\n"));
        for m in side.iter().take(8) {
            out.push_str(&format!(
                "  {:>7}  {:>6}  {}\n",
                m.requests,
                pct(Some(m.request_share)),
                m.model.as_deref().unwrap_or("(none)")
            ));
        }
    }
    out.push_str("\nheaviest fan-outs (max concurrent subagents):\n");
    for s in &r.heaviest_fanouts {
        out.push_str(&format!(
            "  {:>3} concurrent, {:>3} subagents, {:>10} subagent tokens  {} {}  {}\n",
            s.max_concurrent_fanout,
            s.subagent_count,
            s.subagent_tokens,
            s.tool,
            s.session_id,
            s.display_title
                .as_deref()
                .or(s.title.as_deref())
                .unwrap_or("")
        ));
    }
    out.push_str("\ncoverage:\n");
    for (tool, c) in &r.coverage {
        out.push_str(&format!(
            "  {tool}: {}. {}\n",
            if c.supported {
                "supported"
            } else {
                "not supported"
            },
            c.note
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    #[test]
    fn fanout_of_hand_built_intervals() {
        assert_eq!(max_concurrent(&[]), 0);
        assert_eq!(max_concurrent(&[(0, 10)]), 1);
        // Disjoint.
        assert_eq!(max_concurrent(&[(0, 10), (11, 20), (21, 30)]), 1);
        // Two overlap, the third starts after both end.
        assert_eq!(max_concurrent(&[(0, 10), (5, 15), (20, 30)]), 2);
        // Three at once at t = 6..8.
        assert_eq!(max_concurrent(&[(0, 10), (5, 15), (6, 8)]), 3);
        // Touching endpoints overlap (closed intervals).
        assert_eq!(max_concurrent(&[(0, 10), (10, 20)]), 2);
        // Single-instant intervals at the same time overlap.
        assert_eq!(max_concurrent(&[(5, 5), (5, 5), (5, 5)]), 3);
        // A long one spanning several short ones: peak is 2, not the count.
        assert_eq!(max_concurrent(&[(0, 100), (1, 2), (3, 4), (5, 6)]), 2);
        // Input order does not matter.
        assert_eq!(max_concurrent(&[(20, 30), (0, 10), (5, 15)]), 2);
    }

    #[test]
    fn buckets_are_named() {
        let names: Vec<_> = BUCKETS.iter().map(|&(a, b)| bucket_name(a, b)).collect();
        assert_eq!(names, ["0", "1", "2", "3-4", "5-9", "10-19", "20+"]);
    }

    fn put_session(s: &Store, id: &str, sub: bool, parent: Option<&str>) {
        s.conn
            .execute(
                "INSERT INTO sessions (tool, session_id, is_subagent, parent_session_id, title)
                 VALUES ('codex', ?1, ?2, ?3, ?1)",
                params![id, sub as i64, parent],
            )
            .unwrap();
    }

    fn put_req(s: &Store, sid: &str, key: &str, ts: i64, model: &str, effort: Option<&str>) {
        s.conn
            .execute(
                "INSERT INTO requests (tool, request_key, session_id, is_subagent, ts_ms, model,
                     effort, input_tokens, output_tokens)
                 SELECT 'codex', ?1, ?2, is_subagent, ?3, ?4, ?5, 10, 5
                 FROM sessions WHERE tool = 'codex' AND session_id = ?2",
                params![key, sid, ts, model, effort],
            )
            .unwrap();
    }

    #[test]
    fn metrics_per_session_from_rows() {
        let s = Store::open_in_memory().unwrap();
        put_session(&s, "root", false, None);
        put_session(&s, "lonely", false, None);
        put_session(&s, "a", true, Some("root"));
        put_session(&s, "b", true, Some("root"));
        put_session(&s, "b2", true, Some("b")); // nested: still attached to root
        put_session(&s, "orphan", true, None);
        put_req(&s, "root", "r1", 0, "big", None);
        put_req(&s, "a", "a1", 100, "small", Some("low"));
        put_req(&s, "a", "a2", 200, "small", Some("low"));
        put_req(&s, "b", "b1", 150, "mid", Some("high"));
        put_req(&s, "b", "b2", 250, "mid", None);
        put_req(&s, "b2", "c1", 400, "small", Some("low"));
        put_req(&s, "orphan", "o1", 500, "small", None);

        let r = agents_report(&s.conn, None, 0).unwrap();
        assert_eq!(r.totals.top_level_sessions, 2);
        assert_eq!(r.totals.sessions_with_subagents, 1);
        assert_eq!(r.totals.subagent_sessions, 4);
        assert_eq!(r.totals.unlinked_subagent_sessions, 1);
        let top = &r.heaviest_fanouts;
        assert_eq!(top.len(), 1);
        let t = &top[0];
        assert_eq!(t.session_id, "root");
        assert_eq!(t.subagent_count, 3);
        // a: 100-200, b: 150-250, b2: 400-400 -> a and b overlap, b2 is alone.
        assert_eq!(t.max_concurrent_fanout, 2);
        assert_eq!(t.subagent_tokens, 5 * 15);
        assert_eq!(t.total_tokens, 6 * 15);
        assert!((t.subagent_token_share.unwrap() - 5.0 / 6.0).abs() < 1e-12);
        assert_eq!(
            t.subagent_models,
            vec![
                ModelCount {
                    model: Some("small".into()),
                    requests: 3
                },
                ModelCount {
                    model: Some("mid".into()),
                    requests: 2
                },
            ]
        );
        assert_eq!(
            t.subagent_efforts,
            vec![
                EffortCount {
                    effort: Some("low".into()),
                    requests: 3
                },
                EffortCount {
                    effort: Some("high".into()),
                    requests: 1
                },
                EffortCount {
                    effort: None,
                    requests: 1
                },
            ]
        );
        // Histogram: lonely has 0, root has 3 (bucket 3-4).
        let h = |name: &str| {
            r.histogram
                .iter()
                .find(|b| b.bucket == name)
                .unwrap()
                .sessions
        };
        assert_eq!((h("0"), h("3-4")), (1, 1));
        // Global share counts the unlinked subagent too: 6 sub requests of 7.
        assert_eq!(r.subagent_token_share.total_tokens, 7 * 15);
        assert_eq!(r.subagent_token_share.subagent_tokens, 6 * 15);
        assert_eq!(r.model_mix.main_thread.len(), 1);
        assert_eq!(r.model_mix.subagent[0].model.as_deref(), Some("small"));
        assert_eq!(r.by_tool.len(), 1);
        assert_eq!(r.by_tool[0].unlinked_subagent_sessions, 1);

        // A window that starts after "a" drops it and its 100-200 interval.
        let r = agents_report(&s.conn, Some(240), 0).unwrap();
        assert_eq!(r.heaviest_fanouts[0].subagent_count, 2); // b (b at 250, b2 at 400)
        assert_eq!(r.heaviest_fanouts[0].max_concurrent_fanout, 1); // 250 and 400 never overlap
        assert_eq!(
            r.totals.top_level_sessions, 1,
            "only root has in-window subagents"
        );
    }

    #[test]
    fn coverage_names_every_tool() {
        let c = coverage();
        assert!(c.values().all(|v| !v.note.is_empty()));
        assert_eq!(
            c.keys().map(String::as_str).collect::<Vec<_>>(),
            ["antigravity", "claude-code", "codex"]
        );
    }
}
