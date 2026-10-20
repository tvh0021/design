//! Read-only queries behind the dashboard. Every function takes a plain
//! `Connection` so the page can be rendered from an in-memory fixture, and
//! none of them writes. The totals here must agree with `query::summary` for
//! the same window; `totals` uses the same `ts_ms >= since` filter and the
//! same column sums so that holds by construction.

use crate::query::Tokens;
use anyhow::Result;
use chrono::{Datelike, TimeZone, Timelike};
use rusqlite::{Connection, OptionalExtension, params};

/// The three harnesses, in display order. Each owns one hue (docs/design.md).
pub const TOOLS: [&str; 3] = ["claude-code", "codex", "antigravity"];

/// How far back the page looks. Chosen with `?window=`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Window {
    FourWeeks,
    TwelveWeeks,
    All,
}

impl Window {
    pub const ALL: [Window; 3] = [Window::FourWeeks, Window::TwelveWeeks, Window::All];

    /// Query-string value; unknown values fall back to four weeks.
    pub fn parse(s: Option<&str>) -> Window {
        match s {
            Some("12w") => Window::TwelveWeeks,
            Some("all") => Window::All,
            _ => Window::FourWeeks,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Window::FourWeeks => "4w",
            Window::TwelveWeeks => "12w",
            Window::All => "all",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Window::FourWeeks => "4 weeks",
            Window::TwelveWeeks => "12 weeks",
            Window::All => "all time",
        }
    }

    /// The `--since` value that reproduces this window with `query summary`.
    pub fn since_arg(self) -> Option<&'static str> {
        match self {
            Window::FourWeeks => Some("4w"),
            Window::TwelveWeeks => Some("12w"),
            Window::All => None,
        }
    }

    /// Lower bound on `ts_ms`, matching `query::parse_since` arithmetic.
    pub fn since_ms(self, now_ms: i64) -> Option<i64> {
        let week = 7 * 24 * 3_600_000i64;
        match self {
            Window::FourWeeks => Some(now_ms - 4 * week),
            Window::TwelveWeeks => Some(now_ms - 12 * week),
            Window::All => None,
        }
    }
}

/// `?tool=` narrows the page to one harness. Unknown names mean no filter.
pub fn parse_tool(s: Option<&str>) -> Option<&'static str> {
    TOOLS.iter().copied().find(|t| Some(*t) == s)
}

/// One hour of one weekday on the activity map.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct Cell {
    pub requests: i64,
    /// Uncached input plus output: the tokens the map is coloured by.
    pub tokens: i64,
    pub cost_usd: Option<f64>,
    pub unpriced: i64,
}

impl Cell {
    fn add(&mut self, requests: i64, tokens: i64, cost: Option<f64>, unpriced: i64) {
        self.requests += requests;
        self.tokens += tokens;
        if let Some(c) = cost {
            *self.cost_usd.get_or_insert(0.0) += c;
        }
        self.unpriced += unpriced;
    }
}

/// 7 weekdays (Monday first) by 24 local hours.
#[derive(Debug, Clone, PartialEq)]
pub struct Heatmap {
    pub cells: [[Cell; 24]; 7],
    /// Token value at which each shade level 1..=5 begins (quintiles of the
    /// non-empty cells). Empty when every cell is empty.
    pub thresholds: Vec<i64>,
}

impl Heatmap {
    /// Shade level 0..=5 for a cell (0 = empty).
    pub fn level(&self, cell: &Cell) -> usize {
        if cell.requests == 0 {
            return 0;
        }
        self.thresholds
            .iter()
            .filter(|t| cell.tokens >= **t)
            .count()
            .clamp(1, 5)
    }

    pub fn is_empty(&self) -> bool {
        self.thresholds.is_empty()
    }
}

pub const WEEKDAYS: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/// Local weekday (Monday = 0) and hour for an epoch-ms timestamp in `tz`.
/// None for a timestamp the zone cannot represent.
pub fn bucket(ts_ms: i64, tz: &impl TimeZone) -> Option<(usize, usize)> {
    let t = tz.timestamp_millis_opt(ts_ms).single()?;
    Some((
        t.weekday().num_days_from_monday() as usize,
        t.hour() as usize,
    ))
}

/// Quintile start values of the non-empty cells, sorted ascending.
fn quintiles(mut values: Vec<i64>) -> Vec<i64> {
    if values.is_empty() {
        return Vec::new();
    }
    values.sort_unstable();
    let n = values.len();
    (0..5).map(|k| values[k * n / 5]).collect()
}

/// Requests at or after `since_ms` (all when None), for one tool or all,
/// bucketed by local weekday and hour. Requests are grouped into
/// 15-minute UTC buckets in SQL first, which every real zone offset divides
/// evenly, so the per-row conversion stays correct across DST changes while
/// the row count stays small.
pub fn heatmap(
    conn: &Connection,
    since_ms: Option<i64>,
    tool: Option<&str>,
    tz: &impl TimeZone,
) -> Result<Heatmap> {
    let mut stmt = conn.prepare(
        r#"SELECT (ts_ms / 900000) * 900000, COUNT(*),
                  SUM(input_tokens + output_tokens), SUM(cost_usd),
                  COUNT(*) - COUNT(cost_usd)
           FROM requests
           WHERE ts_ms >= ?1 AND (?2 IS NULL OR tool = ?2)
           GROUP BY 1"#,
    )?;
    let mut cells = [[Cell::default(); 24]; 7];
    let rows = stmt.query_map(params![since_ms.unwrap_or(i64::MIN), tool], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, i64>(2)?,
            r.get::<_, Option<f64>>(3)?,
            r.get::<_, i64>(4)?,
        ))
    })?;
    for row in rows {
        let (start, requests, tokens, cost, unpriced) = row?;
        if let Some((d, h)) = bucket(start, tz) {
            cells[d][h].add(requests, tokens, cost, unpriced);
        }
    }
    let thresholds = quintiles(
        cells
            .iter()
            .flatten()
            .filter(|c| c.requests > 0)
            .map(|c| c.tokens)
            .collect(),
    );
    Ok(Heatmap { cells, thresholds })
}

#[derive(Debug, Clone, PartialEq)]
pub struct ToolTotals {
    pub tool: String,
    pub tokens: Tokens,
}

/// Window totals per tool and overall. The overall row equals the `totals`
/// of `query::summary(conn, since_ms, _)` when `tool` is None.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Totals {
    pub by_tool: Vec<ToolTotals>,
    pub total: Tokens,
}

pub fn totals(conn: &Connection, since_ms: Option<i64>, tool: Option<&str>) -> Result<Totals> {
    let mut stmt = conn.prepare(
        r#"SELECT tool, COUNT(*),
                  SUM(input_tokens), SUM(cache_read_input_tokens),
                  SUM(cache_creation_input_tokens), SUM(output_tokens),
                  SUM(cost_usd), COUNT(*) - COUNT(cost_usd)
           FROM requests
           WHERE ts_ms >= ?1 AND (?2 IS NULL OR tool = ?2)
           GROUP BY tool"#,
    )?;
    let mut by_tool = stmt
        .query_map(params![since_ms.unwrap_or(i64::MIN), tool], |r| {
            Ok(ToolTotals {
                tool: r.get(0)?,
                tokens: Tokens {
                    requests: r.get(1)?,
                    input_tokens: r.get(2)?,
                    cache_read_input_tokens: r.get(3)?,
                    cache_creation_input_tokens: r.get(4)?,
                    output_tokens: r.get(5)?,
                    estimated_cost_usd: r.get(6)?,
                    unpriced_requests: r.get(7)?,
                },
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    // Known tools in display order, then anything else alphabetically.
    by_tool.sort_by_key(|t| {
        (
            TOOLS
                .iter()
                .position(|k| *k == t.tool)
                .unwrap_or(TOOLS.len()),
            t.tool.clone(),
        )
    });
    let mut total = Tokens::default();
    for t in &by_tool {
        let k = &t.tokens;
        total.requests += k.requests;
        total.input_tokens += k.input_tokens;
        total.cache_read_input_tokens += k.cache_read_input_tokens;
        total.cache_creation_input_tokens += k.cache_creation_input_tokens;
        total.output_tokens += k.output_tokens;
        total.unpriced_requests += k.unpriced_requests;
        if let Some(c) = k.estimated_cost_usd {
            *total.estimated_cost_usd.get_or_insert(0.0) += c;
        }
    }
    Ok(Totals { by_tool, total })
}

/// What the header says about the store as a whole.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StoreFacts {
    pub sessions: i64,
    pub requests: i64,
    pub first_ts_ms: Option<i64>,
    pub last_ts_ms: Option<i64>,
}

pub fn store_facts(conn: &Connection) -> Result<StoreFacts> {
    let sessions = conn.query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))?;
    let (requests, first, last) = conn
        .query_row(
            "SELECT COUNT(*), MIN(ts_ms), MAX(ts_ms) FROM requests",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?
        .unwrap_or((0, None, None));
    Ok(StoreFacts {
        sessions,
        requests,
        first_ts_ms: first,
        last_ts_ms: last,
    })
}

/// Every task-kind column of the model matrix, in display order.
pub fn task_kinds() -> Vec<String> {
    crate::labels::KINDS
        .iter()
        .chain(std::iter::once(&crate::labels::UNLABELLED))
        .map(|k| k.to_string())
        .collect()
}

/// Model rows shown before the rest are folded into one "other models" row.
pub const MATRIX_ROWS: usize = 12;

/// One canonical model's requests per task kind (`counts` follows `kinds`).
#[derive(Debug, Clone, PartialEq)]
pub struct MatrixRow {
    /// Canonical model; None for requests with no known model.
    pub model: Option<String>,
    /// How many models this row stands for (more than 1 for the folded row).
    pub models: usize,
    pub counts: Vec<i64>,
    pub total: i64,
}

/// Requests by canonical model and task-kind label. Column totals equal the
/// per-label request counts of `query summary --group-by label` for the same
/// window (summed over tools, or that tool's rows when filtered).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ModelMatrix {
    pub kinds: Vec<String>,
    pub rows: Vec<MatrixRow>,
    pub col_totals: Vec<i64>,
    pub total: i64,
    /// Request count at which shade levels 1..=5 begin (quintiles of the
    /// non-empty cells).
    pub thresholds: Vec<i64>,
}

impl ModelMatrix {
    /// Shade level 0..=5 for a cell's request count.
    pub fn level(&self, n: i64) -> usize {
        if n == 0 {
            return 0;
        }
        self.thresholds
            .iter()
            .filter(|t| n >= **t)
            .count()
            .clamp(1, 5)
    }

    /// Requests in the window that carry a task-kind label.
    pub fn labelled(&self) -> i64 {
        self.kinds
            .iter()
            .zip(&self.col_totals)
            .filter(|(k, _)| k.as_str() != crate::labels::UNLABELLED)
            .map(|(_, n)| n)
            .sum()
    }
}

pub fn model_matrix(
    conn: &Connection,
    since_ms: Option<i64>,
    tool: Option<&str>,
) -> Result<ModelMatrix> {
    let sql = format!(
        r#"WITH {},
        q(tool, session_id, model, n) AS (
            SELECT tool, session_id, canonical_model, COUNT(*) FROM requests
            WHERE ts_ms >= ?1 AND (?2 IS NULL OR tool = ?2)
            GROUP BY 1, 2, 3
        )
        SELECT q.model, COALESCE(e.label, '{}'), SUM(q.n)
        FROM q
        LEFT JOIN eff e ON e.tool = q.tool AND e.session_id = q.session_id
        GROUP BY 1, 2"#,
        crate::labels::EFFECTIVE_LABELS,
        crate::labels::UNLABELLED
    );
    let mut kinds = task_kinds();
    let mut cells: Vec<(Option<String>, usize, i64)> = Vec::new();
    let mut stmt = conn.prepare(&sql)?;
    let mut rows = stmt.query(params![since_ms.unwrap_or(i64::MIN), tool])?;
    while let Some(r) = rows.next()? {
        let label: String = r.get(1)?;
        // A label outside the known kinds still gets a column, so the
        // column totals keep adding up to the label summary.
        let k = match kinds.iter().position(|k| *k == label) {
            Some(k) => k,
            None => {
                kinds.push(label);
                kinds.len() - 1
            }
        };
        cells.push((r.get(0)?, k, r.get(2)?));
    }
    let width = kinds.len();
    let mut by_model: std::collections::BTreeMap<Option<String>, Vec<i64>> = Default::default();
    for (model, k, n) in cells {
        by_model.entry(model).or_insert_with(|| vec![0; width])[k] += n;
    }
    let mut all: Vec<MatrixRow> = by_model
        .into_iter()
        .map(|(model, mut counts)| {
            counts.resize(width, 0);
            MatrixRow {
                model,
                models: 1,
                total: counts.iter().sum(),
                counts,
            }
        })
        .collect();
    // Most requests first; the unknown-model row goes last among equals.
    all.sort_by(|a, b| {
        b.total
            .cmp(&a.total)
            .then_with(|| (a.model.is_none(), &a.model).cmp(&(b.model.is_none(), &b.model)))
    });
    if all.len() > MATRIX_ROWS {
        let rest = all.split_off(MATRIX_ROWS - 1);
        let mut folded = MatrixRow {
            model: None,
            models: rest.len(),
            counts: vec![0; width],
            total: 0,
        };
        for r in rest {
            for (a, b) in folded.counts.iter_mut().zip(&r.counts) {
                *a += b;
            }
            folded.total += r.total;
        }
        all.push(folded);
    }
    let mut col_totals = vec![0; width];
    for r in &all {
        for (a, b) in col_totals.iter_mut().zip(&r.counts) {
            *a += b;
        }
    }
    let thresholds = quintiles(
        all.iter()
            .flat_map(|r| r.counts.iter().copied())
            .filter(|n| *n > 0)
            .collect(),
    );
    Ok(ModelMatrix {
        total: col_totals.iter().sum(),
        kinds,
        rows: all,
        col_totals,
        thresholds,
    })
}

/// (tool, requests, estimated cost) per tool, in display order.
pub type ToolMix = Vec<(String, i64, Option<f64>)>;

/// One project row of the per-project panel: the `query projects` row plus
/// active time and the tool mix.
#[derive(Debug, Clone, PartialEq)]
pub struct ProjectLine {
    pub row: crate::project::ProjectRow,
    /// Sum of cached `active_seconds` over the
    /// project's top-level sessions in the window.
    pub active_seconds: i64,
    /// (tool, requests, estimated cost) for each tool with requests here,
    /// in display order.
    pub tools: ToolMix,
}

/// Projects shown, most estimated cost first.
pub const PROJECT_ROWS: usize = 10;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Projects {
    pub lines: Vec<ProjectLine>,
    /// Projects with activity in the window, before the cut.
    pub count: usize,
}

/// Elapsed seconds of a session as `query session` reports it: its own
/// recorded start and end, else its first and last request in the window.
const ELAPSED_SQL: &str =
    "MAX(0, (COALESCE(s.ended_at_ms, w.l) - COALESCE(s.started_at_ms, w.f)) / 1000)";

/// Per-session request figures in the window, as CTE `w`.
const WINDOW_CTE: &str = r#"w(tool, session_id, n, cost, unpriced, f, l) AS (
    SELECT tool, session_id, COUNT(*), SUM(cost_usd), COUNT(*) - COUNT(cost_usd),
           MIN(ts_ms), MAX(ts_ms)
    FROM requests WHERE ts_ms >= ?1 AND (?2 IS NULL OR tool = ?2)
    GROUP BY tool, session_id
)"#;

pub fn projects(
    conn: &Connection,
    since_ms: Option<i64>,
    tool: Option<&str>,
    now_ms: i64,
) -> Result<Projects> {
    let (list, by_tool) = crate::project::list_with_tools(conn, since_ms, tool, now_ms)?;
    let mut rows: Vec<_> = list
        .projects
        .into_iter()
        .filter(|p| p.sessions > 0 || p.tokens.requests > 0)
        .collect();
    let count = rows.len();
    rows.sort_by(|a, b| {
        b.tokens
            .estimated_cost_usd
            .unwrap_or(0.0)
            .total_cmp(&a.tokens.estimated_cost_usd.unwrap_or(0.0))
            .then(b.tokens.requests.cmp(&a.tokens.requests))
            .then(a.project.cmp(&b.project))
    });
    rows.truncate(PROJECT_ROWS);

    let mut mix: std::collections::HashMap<Option<String>, ToolMix> = Default::default();
    for (project, t) in by_tool {
        if t.tokens.requests == 0 {
            continue;
        }
        mix.entry(project).or_default().push((
            t.tool,
            t.tokens.requests,
            t.tokens.estimated_cost_usd,
        ));
    }
    let lines = rows
        .into_iter()
        .map(|row| {
            let mut tools = mix.remove(&row.project).unwrap_or_default();
            tools.sort_by_key(|(t, _, _)| tool_order(t));
            ProjectLine {
                active_seconds: row.active_seconds,
                tools,
                row,
            }
        })
        .collect();
    Ok(Projects { lines, count })
}

/// Display position of a tool: known tools in order, others after.
pub fn tool_order(tool: &str) -> (usize, String) {
    (
        TOOLS.iter().position(|k| *k == tool).unwrap_or(TOOLS.len()),
        tool.to_string(),
    )
}

/// One top-level session in the longest and costliest lists.
#[derive(Debug, Clone, PartialEq)]
pub struct SessionLine {
    pub tool: String,
    pub session_id: String,
    /// `sessions.title`: the first line of the opening prompt, at most 120
    /// characters, stored at ingest. No other prompt text is read.
    pub title: Option<String>,
    pub project: Option<String>,
    pub project_name: Option<String>,
    /// As `query session`'s `elapsed_seconds` (the whole session, not the window).
    pub elapsed_seconds: i64,
    pub active_seconds: i64,
    /// Requests in the window (the session's own transcript).
    pub requests: i64,
    pub cost_usd: Option<f64>,
    pub unpriced: i64,
}

/// Rows in each of the two session lists.
pub const SESSION_ROWS: usize = 8;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct SessionLists {
    pub longest: Vec<SessionLine>,
    pub costliest: Vec<SessionLine>,
}

/// Top-level sessions with a request in the window, by active time and by
/// estimated cost. Figures match `query session <id> --since <window>`.
pub fn session_lists(
    conn: &Connection,
    since_ms: Option<i64>,
    tool: Option<&str>,
) -> Result<SessionLists> {
    let run = |order: &str| -> Result<Vec<SessionLine>> {
        let sql = format!(
            r#"WITH {WINDOW_CTE}
            SELECT s.tool, s.session_id, s.title, s.project, s.project_name,
                   {ELAPSED_SQL} AS elapsed, w.n, w.cost, w.unpriced, COALESCE(s.active_ms / 1000, 0) AS active
            FROM w JOIN sessions s ON s.tool = w.tool AND s.session_id = w.session_id
            WHERE NOT EXISTS (SELECT 1 FROM sessions p
                WHERE p.tool = COALESCE(s.parent_tool, s.tool) AND p.session_id = s.parent_session_id)
            ORDER BY {order}, s.tool, s.session_id
            LIMIT {SESSION_ROWS}"#
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt
            .query_map(params![since_ms.unwrap_or(i64::MIN), tool], |r| {
                Ok(SessionLine {
                    tool: r.get(0)?,
                    session_id: r.get(1)?,
                    title: r.get(2)?,
                    project: r.get(3)?,
                    project_name: r.get(4)?,
                    elapsed_seconds: r.get(5)?,
                    active_seconds: r.get(9)?,
                    requests: r.get(6)?,
                    cost_usd: r.get(7)?,
                    unpriced: r.get(8)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    };
    Ok(SessionLists {
        longest: run("active DESC, w.cost DESC")?,
        costliest: {
            let mut v = run("w.cost DESC, active DESC")?;
            v.retain(|s| s.cost_usd.is_some());
            v
        },
    })
}

/// (estimated cost, unpriced requests) of one spend cell.
pub type CostCell = (Option<f64>, i64);

/// One row of the condensed spend table: a week or a month.
#[derive(Debug, Clone, PartialEq)]
pub struct SpendPeriod {
    /// "from 22 Sep" or "Oct 2026"
    pub label: String,
    /// One entry per tool in `Spend::tools`: (est. cost, unpriced requests).
    pub cells: Vec<CostCell>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Spend {
    pub by_month: bool,
    pub tools: Vec<String>,
    /// Oldest first.
    pub periods: Vec<SpendPeriod>,
}

/// Exact aggregates are computed before any public rounding.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct SpendTotals {
    pub rows: Vec<CostCell>,
    pub columns: Vec<CostCell>,
    pub all: CostCell,
}

impl Spend {
    pub fn totals(&self) -> SpendTotals {
        let mut totals = SpendTotals {
            columns: vec![(None, 0); self.tools.len()],
            ..Default::default()
        };
        for period in &self.periods {
            let mut row = (None, 0);
            for (i, &(cost, unpriced)) in period.cells.iter().enumerate() {
                add_cost(&mut row, cost, unpriced);
                add_cost(&mut totals.columns[i], cost, unpriced);
                add_cost(&mut totals.all, cost, unpriced);
            }
            totals.rows.push(row);
        }
        totals
    }
}

fn add_cost(cell: &mut CostCell, cost: Option<f64>, unpriced: i64) {
    if let Some(c) = cost {
        *cell.0.get_or_insert(0.0) += c;
    }
    cell.1 += unpriced;
}

/// Estimated cost per tool by week of the window (weeks counted back from
/// `now_ms`, so they tile the window exactly), or by local calendar month
/// for all time. Each tool's column adds up to its window total.
pub fn spend(
    conn: &Connection,
    window: Window,
    now_ms: i64,
    tool: Option<&str>,
    tz: &impl TimeZone,
) -> Result<Spend> {
    let tools: Vec<String> = match tool {
        Some(t) => vec![t.to_string()],
        None => {
            let mut v: Vec<String> = conn
                .prepare("SELECT DISTINCT tool FROM requests")?
                .query_map([], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            v.sort_by_key(|t| tool_order(t));
            v
        }
    };
    let col = |t: &str| tools.iter().position(|x| x == t);
    let week = 7 * 24 * 3_600_000i64;
    if let Some(since) = window.since_ms(now_ms) {
        let n = ((now_ms - since) / week) as usize;
        let mut periods: Vec<SpendPeriod> = (0..n)
            .rev()
            .map(|k| SpendPeriod {
                label: format!(
                    "from {}",
                    tz.timestamp_millis_opt(now_ms - (k as i64 + 1) * week)
                        .single()
                        .map(|t| t.fixed_offset().format("%-d %b").to_string())
                        .unwrap_or_default()
                ),
                cells: vec![(None, 0); tools.len()],
            })
            .collect();
        let mut stmt = conn.prepare(
            r#"SELECT tool, MAX(0, (?3 - ts_ms) / ?4), SUM(cost_usd), COUNT(*) - COUNT(cost_usd)
               FROM requests WHERE ts_ms >= ?1 AND (?2 IS NULL OR tool = ?2)
               GROUP BY 1, 2"#,
        )?;
        let mut rows = stmt.query(params![since, tool, now_ms, week])?;
        while let Some(r) = rows.next()? {
            let t: String = r.get(0)?;
            let back = (r.get::<_, i64>(1)? as usize).min(n - 1);
            if let Some(c) = col(&t) {
                add_cost(&mut periods[n - 1 - back].cells[c], r.get(2)?, r.get(3)?);
            }
        }
        return Ok(Spend {
            by_month: false,
            tools,
            periods,
        });
    }
    // All time: local months, from 15-minute UTC buckets as in `heatmap`.
    let mut stmt = conn.prepare(
        r#"SELECT (ts_ms / 900000) * 900000, tool, SUM(cost_usd), COUNT(*) - COUNT(cost_usd)
           FROM requests WHERE ?1 IS NULL OR tool = ?1
           GROUP BY 1, 2"#,
    )?;
    let mut months: std::collections::BTreeMap<(i32, u32), Vec<CostCell>> = Default::default();
    let mut rows = stmt.query(params![tool])?;
    while let Some(r) = rows.next()? {
        let start: i64 = r.get(0)?;
        let t: String = r.get(1)?;
        let (Some(local), Some(c)) = (tz.timestamp_millis_opt(start).single(), col(&t)) else {
            continue;
        };
        let cells = months
            .entry((local.year(), local.month()))
            .or_insert_with(|| vec![(None, 0); tools.len()]);
        add_cost(&mut cells[c], r.get(2)?, r.get(3)?);
    }
    // Fill the gaps so the months read as a continuous ledger.
    let mut periods = Vec::new();
    if let (Some(first), Some(last)) = (
        months.keys().next().copied(),
        months.keys().next_back().copied(),
    ) {
        let (mut y, mut m) = first;
        while (y, m) <= last {
            let cells = months
                .remove(&(y, m))
                .unwrap_or_else(|| vec![(None, 0); tools.len()]);
            let label = chrono::NaiveDate::from_ymd_opt(y, m, 1)
                .map(|d| d.format("%b %Y").to_string())
                .unwrap_or_default();
            periods.push(SpendPeriod { label, cells });
            (y, m) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
        }
    }
    Ok(Spend {
        by_month: true,
        tools,
        periods,
    })
}

/// Cache reads as a share of all input (uncached + cache read + cache write);
/// None when there is no input at all.
pub fn cache_read_ratio(t: &Tokens) -> Option<f64> {
    let all = t.input_tokens + t.cache_read_input_tokens + t.cache_creation_input_tokens;
    (all > 0).then(|| t.cache_read_input_tokens as f64 / all as f64)
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use chrono::FixedOffset;

    #[test]
    fn bucket_uses_local_weekday_and_hour() {
        // 2026-10-19T03:30:00Z is a Monday. In UTC-4 it is Sunday 23:30;
        // in UTC+9 it is Monday 12:30.
        let ts = 1_792_380_600_000i64;
        let utc = FixedOffset::east_opt(0).unwrap();
        assert_eq!(bucket(ts, &utc), Some((0, 3)));
        let ny = FixedOffset::west_opt(4 * 3600).unwrap();
        assert_eq!(bucket(ts, &ny), Some((6, 23)));
        let tokyo = FixedOffset::east_opt(9 * 3600).unwrap();
        assert_eq!(bucket(ts, &tokyo), Some((0, 12)));
        // A 30-minute zone (India) lands in the right local hour too.
        let india = FixedOffset::east_opt(5 * 3600 + 1800).unwrap();
        assert_eq!(bucket(ts, &india), Some((0, 9)));
    }

    #[test]
    fn heatmap_sums_cells_and_levels_by_quintile() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let tz = FixedOffset::east_opt(0).unwrap();
        let base = 1_792_380_600_000i64; // Monday 03:30 UTC
        let hour = 3_600_000i64;
        // Ten cells with tokens 1..=10, each one request; plus two requests
        // in the same quarter-hour and two in the same hour but different
        // quarters, all on Monday 03:00.
        let mut rows = Vec::new();
        for i in 1..=10i64 {
            rows.push((base + i * hour, "codex", i, 0, Some(0.5)));
        }
        rows.push((base, "codex", 100, 0, None));
        rows.push((base + 60_000, "claude-code", 100, 0, Some(1.0)));
        rows.push((base + 20 * 60_000, "codex", 0, 100, Some(2.0)));
        for (i, (ts, tool, inp, out, cost)) in rows.iter().enumerate() {
            store
                .conn
                .execute(
                    "INSERT INTO requests (tool, request_key, session_id, ts_ms, input_tokens,
                         output_tokens, cost_usd) VALUES (?1, ?2, 's', ?3, ?4, ?5, ?6)",
                    params![tool, format!("k{i}"), ts, inp, out, cost],
                )
                .unwrap();
        }
        let h = heatmap(&store.conn, None, None, &tz).unwrap();
        let c = h.cells[0][3];
        assert_eq!(
            c,
            Cell {
                requests: 3,
                tokens: 300,
                cost_usd: Some(3.0),
                unpriced: 1
            }
        );
        assert_eq!(h.level(&c), 5);
        assert_eq!(h.level(&Cell::default()), 0);
        // Eleven non-empty cells: tokens 1..=10 and 300. Quintile starts at
        // sorted indexes 0, 2, 4, 6, 8.
        assert_eq!(h.thresholds, vec![1, 3, 5, 7, 9]);
        let one = h.cells[0][4]; // Monday 04:00: tokens 1
        assert_eq!(one.tokens, 1);
        assert_eq!(h.level(&one), 1);
        assert_eq!(
            h.level(&Cell {
                tokens: 8,
                requests: 1,
                ..Cell::default()
            }),
            4
        );

        // The tool filter drops the Claude Code request.
        let codex = heatmap(&store.conn, None, Some("codex"), &tz).unwrap();
        assert_eq!(codex.cells[0][3].requests, 2);
        // A since bound drops the three 03:00 requests.
        let later = heatmap(&store.conn, Some(base + hour), None, &tz).unwrap();
        assert_eq!(later.cells[0][3].requests, 0);
        assert_eq!(later.cells[0][4].requests, 1);

        // Totals agree with query::summary for the same window.
        let t = totals(&store.conn, Some(base + hour), None).unwrap();
        let s = crate::query::summary(&store.conn, Some(base + hour), base).unwrap();
        assert_eq!(t.total, s.totals);
        let t = totals(&store.conn, None, None).unwrap();
        let s = crate::query::summary(&store.conn, None, base).unwrap();
        assert_eq!(t.total, s.totals);
        assert_eq!(
            t.by_tool
                .iter()
                .map(|t| t.tool.as_str())
                .collect::<Vec<_>>(),
            vec!["claude-code", "codex"]
        );
        assert_eq!(t.by_tool[0].tokens.requests, 1);
    }

    #[test]
    fn quintiles_of_few_values() {
        assert_eq!(quintiles(vec![]), Vec::<i64>::new());
        assert_eq!(quintiles(vec![7]), vec![7, 7, 7, 7, 7]);
        assert_eq!(quintiles(vec![3, 1, 2]), vec![1, 1, 2, 2, 3]);
    }

    #[test]
    fn window_and_tool_parsing() {
        assert_eq!(Window::parse(None), Window::FourWeeks);
        assert_eq!(Window::parse(Some("12w")), Window::TwelveWeeks);
        assert_eq!(Window::parse(Some("all")), Window::All);
        assert_eq!(Window::parse(Some("junk")), Window::FourWeeks);
        let now = 10_000_000_000i64;
        assert_eq!(
            Window::FourWeeks.since_ms(now),
            Some(now - crate::query::parse_since("4w").unwrap())
        );
        assert_eq!(Window::All.since_ms(now), None);
        assert_eq!(parse_tool(Some("codex")), Some("codex"));
        assert_eq!(parse_tool(Some("vim")), None);
        assert_eq!(parse_tool(None), None);
    }

    pub(crate) const NOW: i64 = 1_792_400_000_000;
    const DAY: i64 = 86_400_000;

    /// Two tools plus an Antigravity session outside the 4-week window;
    /// rule and hand-set labels, an unlabelled session, nested and unlinked
    /// subagents, a session with no directory and one with no recorded span,
    /// and requests with and without a price and a canonical model.
    #[allow(clippy::type_complexity)]
    pub(crate) fn panel_fixture() -> crate::store::Store {
        let store = crate::store::Store::open_in_memory().unwrap();
        let sessions: &[(
            &str,
            &str,
            i64,
            Option<&str>,
            Option<&str>,
            Option<&str>,
            Option<i64>,
            Option<i64>,
        )] = &[
            // tool, id, is_subagent, parent, label, project, started, ended
            (
                "claude-code",
                "c1",
                0,
                None,
                Some("coding"),
                Some("/home/demo/alpha"),
                Some(NOW - 10 * DAY),
                Some(NOW - 10 * DAY + 7_200_000),
            ),
            (
                "claude-code",
                "c1-a",
                1,
                Some("c1"),
                None,
                Some("/home/demo/alpha"),
                Some(NOW - 10 * DAY),
                Some(NOW - 10 * DAY + 600_000),
            ),
            (
                "claude-code",
                "c1-b",
                1,
                Some("c1-a"),
                None,
                Some("/home/demo/alpha"),
                Some(NOW - 10 * DAY + 60_000),
                Some(NOW - 10 * DAY + 900_000),
            ),
            (
                "claude-code",
                "c2",
                0,
                None,
                Some("writing"),
                Some("/home/demo/alpha"),
                Some(NOW - 2 * DAY),
                Some(NOW - 2 * DAY + 1_800_000),
            ),
            (
                "codex",
                "x1",
                0,
                None,
                Some("research"),
                Some("/home/demo/beta"),
                Some(NOW - 40 * DAY),
                Some(NOW - 3 * DAY),
            ),
            ("codex", "x2", 0, None, None, None, None, None),
            (
                "codex",
                "x3",
                1,
                Some("gone"),
                None,
                Some("/home/demo/beta"),
                Some(NOW - 5 * DAY),
                Some(NOW - 5 * DAY + 60_000),
            ),
            (
                "antigravity",
                "g1",
                0,
                None,
                Some("debugging"),
                Some("/home/demo/alpha"),
                Some(NOW - 70 * DAY),
                Some(NOW - 69 * DAY),
            ),
        ];
        for (tool, id, sub, parent, label, project, started, ended) in sessions {
            store
                .conn
                .execute(
                    "INSERT INTO sessions (tool, session_id, is_subagent, parent_session_id, label,
                         label_source, project, project_name, started_at_ms, ended_at_ms, title)
                     VALUES (?1, ?2, ?3, ?4, ?5, CASE WHEN ?5 IS NULL THEN NULL ELSE 'rule' END,
                         ?6, ?7, ?8, ?9, ?10)",
                    params![
                        tool,
                        id,
                        sub,
                        parent,
                        label,
                        project,
                        project.map(crate::project::project_name),
                        started,
                        ended,
                        format!("Demo task {id}")
                    ],
                )
                .unwrap();
        }
        store
            .conn
            .execute(
                "UPDATE sessions SET active_ms = MAX(0, ended_at_ms - started_at_ms) / 2",
                [],
            )
            .unwrap();
        store
            .conn
            .execute(
                "UPDATE sessions SET active_ms = 1200000 WHERE session_id = 'x1'",
                [],
            )
            .unwrap();
        // A hand-set label wins over the rule label.
        store
            .conn
            .execute(
                "INSERT INTO session_labels (tool, session_id, label, set_at) VALUES ('codex', 'x1', 'review', 0)",
                [],
            )
            .unwrap();
        let reqs: &[(
            &str,
            &str,
            i64,
            i64,
            Option<&str>,
            i64,
            i64,
            i64,
            i64,
            Option<f64>,
        )] = &[
            // tool, session, sub, ts, canonical, in, cache read, cache write, out, cost
            (
                "claude-code",
                "c1",
                0,
                NOW - 10 * DAY,
                Some("m-big"),
                100,
                1000,
                50,
                20,
                Some(0.5),
            ),
            (
                "claude-code",
                "c1",
                0,
                NOW - 10 * DAY + 3_600_000,
                Some("m-big"),
                120,
                2000,
                0,
                30,
                Some(0.75),
            ),
            (
                "claude-code",
                "c1-a",
                1,
                NOW - 10 * DAY + 10_000,
                Some("m-small"),
                40,
                300,
                10,
                5,
                Some(0.05),
            ),
            (
                "claude-code",
                "c1-a",
                1,
                NOW - 10 * DAY + 500_000,
                Some("m-small"),
                40,
                300,
                0,
                5,
                Some(0.05),
            ),
            (
                "claude-code",
                "c1-b",
                1,
                NOW - 10 * DAY + 100_000,
                Some("m-small"),
                30,
                200,
                0,
                5,
                None,
            ),
            (
                "claude-code",
                "c2",
                0,
                NOW - 2 * DAY,
                Some("m-big"),
                300,
                4000,
                200,
                60,
                Some(2.0),
            ),
            (
                "codex",
                "x1",
                0,
                NOW - 40 * DAY,
                Some("m-code"),
                500,
                5000,
                0,
                90,
                Some(1.25),
            ),
            (
                "codex",
                "x1",
                0,
                NOW - 3 * DAY,
                Some("m-code"),
                600,
                6000,
                0,
                80,
                Some(1.5),
            ),
            ("codex", "x2", 0, NOW - DAY, None, 70, 0, 0, 7, None),
            (
                "codex",
                "x2",
                0,
                NOW - DAY + 900_000,
                Some("m-code"),
                80,
                800,
                0,
                8,
                Some(0.2),
            ),
            (
                "codex",
                "x3",
                1,
                NOW - 5 * DAY,
                Some("m-code"),
                10,
                100,
                0,
                1,
                Some(0.01),
            ),
            (
                "antigravity",
                "g1",
                0,
                NOW - 70 * DAY,
                Some("m-gem"),
                900,
                0,
                0,
                99,
                Some(0.9),
            ),
        ];
        for (i, (tool, sid, sub, ts, model, inp, cr, cc, out, cost)) in reqs.iter().enumerate() {
            store
                .conn
                .execute(
                    "INSERT INTO requests (tool, request_key, session_id, is_subagent, ts_ms, model,
                         canonical_model, input_tokens, cache_read_input_tokens,
                         cache_creation_input_tokens, output_tokens, cost_usd)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?6, ?7, ?8, ?9, ?10, ?11)",
                    params![tool, format!("r{i}"), sid, sub, ts, model, inp, cr, cc, out, cost],
                )
                .unwrap();
        }
        store
    }

    fn close(a: Option<f64>, b: Option<f64>) -> bool {
        match (a, b) {
            (Some(x), Some(y)) => (x - y).abs() < 1e-9,
            (None, None) => true,
            _ => false,
        }
    }

    /// Every window and tool scope the page offers.
    fn scopes() -> Vec<(Window, Option<&'static str>)> {
        let mut v = Vec::new();
        for w in Window::ALL {
            v.push((w, None));
            for t in TOOLS {
                v.push((w, Some(t)));
            }
        }
        v
    }

    #[test]
    fn model_matrix_columns_match_label_summary() {
        let store = panel_fixture();
        for (w, tool) in scopes() {
            let since = w.since_ms(NOW);
            let m = model_matrix(&store.conn, since, tool).unwrap();
            let s = crate::labels::summary_by_label(&store.conn, since, NOW).unwrap();
            let mut want: std::collections::BTreeMap<String, i64> = Default::default();
            for g in s.groups.iter().filter(|g| tool.is_none_or(|t| t == g.tool)) {
                *want.entry(g.label.clone()).or_default() += g.tokens.requests;
            }
            let got: std::collections::BTreeMap<String, i64> = m
                .kinds
                .iter()
                .cloned()
                .zip(m.col_totals.iter().copied())
                .filter(|(_, n)| *n > 0)
                .collect();
            assert_eq!(got, want, "{w:?} {tool:?}");
            assert_eq!(m.total, want.values().sum::<i64>());
            for r in &m.rows {
                assert_eq!(r.total, r.counts.iter().sum::<i64>());
            }
        }
        // All time, all tools: the hand-set label wins, subagents inherit
        // their root's label through two levels, and the unknown model has a row.
        let m = model_matrix(&store.conn, None, None).unwrap();
        let col = |k: &str| m.kinds.iter().position(|x| x == k).unwrap();
        let row = |name: Option<&str>| m.rows.iter().find(|r| r.model.as_deref() == name).unwrap();
        assert_eq!(row(Some("m-small")).counts[col("coding")], 3);
        assert_eq!(row(Some("m-code")).counts[col("review")], 2);
        assert_eq!(row(Some("m-code")).counts[col("research")], 0);
        assert_eq!(row(Some("m-code")).counts[col("unlabelled")], 2);
        assert_eq!(row(None).counts[col("unlabelled")], 1);
        assert_eq!(m.kinds.len(), 9);
        assert_eq!(m.level(0), 0);
        assert!(m.labelled() > 0);
    }

    #[test]
    fn model_matrix_folds_the_long_tail() {
        let store = crate::store::Store::open_in_memory().unwrap();
        store
            .conn
            .execute(
                "INSERT INTO sessions (tool, session_id, label) VALUES ('codex', 's', 'coding')",
                [],
            )
            .unwrap();
        for i in 0..(MATRIX_ROWS + 3) {
            for k in 0..=i {
                store
                    .conn
                    .execute(
                        "INSERT INTO requests (tool, request_key, session_id, ts_ms, canonical_model)
                         VALUES ('codex', ?1, 's', 1, ?2)",
                        params![format!("{i}-{k}"), format!("m{i:02}")],
                    )
                    .unwrap();
            }
        }
        let m = model_matrix(&store.conn, None, None).unwrap();
        assert_eq!(m.rows.len(), MATRIX_ROWS);
        let folded = m.rows.last().unwrap();
        assert_eq!(folded.models, 4);
        // The four smallest models have 1..=4 requests.
        assert_eq!(folded.total, 1 + 2 + 3 + 4);
        let n = (MATRIX_ROWS + 3) as i64;
        assert_eq!(m.total, n * (n + 1) / 2);
    }

    #[test]
    fn agents_filter_matches_query_agents() {
        let store = panel_fixture();
        for w in Window::ALL {
            let since = w.since_ms(NOW);
            let full = crate::agents::agents_report(&store.conn, since, NOW).unwrap();
            let same = crate::agents::agents_report_for(&store.conn, since, None, NOW).unwrap();
            assert_eq!(
                serde_json::to_value(&full).unwrap(),
                serde_json::to_value(&same).unwrap()
            );
            for t in TOOLS {
                let one =
                    crate::agents::agents_report_for(&store.conn, since, Some(t), NOW).unwrap();
                let want = full.by_tool.iter().find(|r| r.tool == t);
                assert_eq!(one.by_tool.first(), want, "{w:?} {t}");
                assert_eq!(
                    one.totals.top_level_sessions,
                    want.map_or(0, |r| r.top_level_sessions)
                );
                assert_eq!(
                    one.totals.unlinked_subagent_sessions,
                    want.map_or(0, |r| r.unlinked_subagent_sessions)
                );
                let fan: Vec<_> = full
                    .heaviest_fanouts
                    .iter()
                    .filter(|f| f.tool == t)
                    .collect();
                assert_eq!(one.heaviest_fanouts.iter().collect::<Vec<_>>(), fan);
                assert_eq!(
                    one.histogram.iter().map(|b| b.sessions).sum::<usize>(),
                    want.map_or(0, |r| r.top_level_sessions)
                );
            }
        }
        let full = crate::agents::agents_report(&store.conn, None, NOW).unwrap();
        assert_eq!(full.heaviest_fanouts[0].subagent_count, 2);
        assert_eq!(full.totals.unlinked_subagent_sessions, 1);
    }

    #[test]
    fn projects_match_query_projects_and_session_active() {
        let store = panel_fixture();
        for (w, tool) in scopes() {
            let since = w.since_ms(NOW);
            let p = projects(&store.conn, since, tool, NOW).unwrap();
            let list = crate::project::list(&store.conn, since, NOW).unwrap();
            for line in &p.lines {
                let row = &line.row;
                match tool {
                    None => {
                        let want = list
                            .projects
                            .iter()
                            .find(|r| r.project == row.project)
                            .unwrap();
                        assert_eq!(row, want, "{w:?}");
                    }
                    Some(t) => {
                        if let Some(proj) = &row.project {
                            let d = crate::project::detail(&store.conn, proj, since, NOW).unwrap();
                            let want = d.tools.iter().find(|x| x.tool == t).unwrap();
                            assert_eq!(row.sessions, want.sessions);
                            assert_eq!(row.tokens, want.tokens);
                        }
                    }
                }
                // Tool mix adds up to the project's requests.
                assert_eq!(
                    line.tools.iter().map(|t| t.1).sum::<i64>(),
                    row.tokens.requests
                );
                // Active time is the sum of `query session` active time over the
                // project's top-level sessions that `query projects` counts.
                let mut stmt = store
                    .conn
                    .prepare(
                        "SELECT tool, session_id FROM sessions s WHERE NOT EXISTS (
                            SELECT 1 FROM sessions p WHERE p.tool = s.tool
                            AND p.session_id = s.parent_session_id)
                         AND project IS ?1 AND (?2 IS NULL OR tool = ?2)
                         AND (?3 IS NULL OR COALESCE(ended_at_ms, started_at_ms) >= ?3)",
                    )
                    .unwrap();
                let ids: Vec<(String, String)> = stmt
                    .query_map(params![row.project, tool, since], |r| {
                        Ok((r.get(0)?, r.get(1)?))
                    })
                    .unwrap()
                    .collect::<rusqlite::Result<_>>()
                    .unwrap();
                let want: i64 = ids
                    .iter()
                    .map(|(t, id)| {
                        let r = crate::query::SessionRef {
                            tool: t.clone(),
                            session_id: id.clone(),
                            matched_by: crate::query::MatchedBy::SessionId,
                        };
                        crate::query::session_report(&store.conn, &r, since, false, NOW)
                            .unwrap()
                            .active_seconds
                            .unwrap_or(0)
                    })
                    .sum();
                assert_eq!(
                    line.active_seconds, want,
                    "{w:?} {tool:?} {:?}",
                    row.project
                );
            }
            if tool.is_none() {
                assert_eq!(p.count, list.projects.len());
            }
        }
        // Costliest first; the no-directory row is present for codex.
        let p = projects(&store.conn, None, None, NOW).unwrap();
        assert_eq!(p.lines[0].row.project.as_deref(), Some("/home/demo/alpha"));
        assert_eq!(p.lines[0].active_seconds, (7_200 + 1_800 + DAY / 1000) / 2);
        assert!(p.lines.iter().any(|l| l.row.project.is_none()));
    }

    #[test]
    fn session_lists_match_query_session() {
        let store = panel_fixture();
        for (w, tool) in scopes() {
            let since = w.since_ms(NOW);
            let l = session_lists(&store.conn, since, tool).unwrap();
            for s in l.longest.iter().chain(&l.costliest) {
                assert!(tool.is_none_or(|t| t == s.tool));
                let r = crate::query::SessionRef {
                    tool: s.tool.clone(),
                    session_id: s.session_id.clone(),
                    matched_by: crate::query::MatchedBy::SessionId,
                };
                let rep = crate::query::session_report(&store.conn, &r, since, false, NOW).unwrap();
                assert!(!rep.is_subagent || rep.parent_session_id.as_deref() == Some("gone"));
                assert_eq!(s.active_seconds, rep.active_seconds.unwrap_or(0));
                assert_eq!(Some(s.elapsed_seconds), rep.elapsed_seconds);
                assert_eq!(s.requests, rep.usage.tokens.requests);
                assert!(close(s.cost_usd, rep.usage.tokens.estimated_cost_usd));
                assert_eq!(s.unpriced, rep.usage.tokens.unpriced_requests);
                assert_eq!(s.title, rep.title);
            }
            for pair in l.longest.windows(2) {
                assert!(pair[0].active_seconds >= pair[1].active_seconds);
            }
        }
        let l = session_lists(&store.conn, None, None).unwrap();
        // x1 spans 37 days but has only 20 active minutes; g1 is active longest.
        assert_eq!(l.longest[0].session_id, "g1");
        // x2 has no recorded span and falls back to its requests.
        assert_eq!(
            l.longest
                .iter()
                .find(|s| s.session_id == "x2")
                .unwrap()
                .elapsed_seconds,
            900
        );
        assert_eq!(l.costliest[0].session_id, "x1");
        assert_eq!(l.longest.len(), 6);
    }

    #[test]
    fn cache_ratio_matches_summary() {
        let store = panel_fixture();
        let t = totals(&store.conn, None, None).unwrap();
        let s = crate::query::summary(&store.conn, None, NOW).unwrap();
        for row in &t.by_tool {
            let (mut inp, mut cr, mut cc) = (0, 0, 0);
            for g in s.groups.iter().filter(|g| g.tool == row.tool) {
                inp += g.tokens.input_tokens;
                cr += g.tokens.cache_read_input_tokens;
                cc += g.tokens.cache_creation_input_tokens;
            }
            let want = (inp + cr + cc > 0).then(|| cr as f64 / (inp + cr + cc) as f64);
            assert!(close(cache_read_ratio(&row.tokens), want));
        }
        assert_eq!(cache_read_ratio(&Tokens::default()), None);
        let cc = &t.by_tool[0].tokens; // claude-code: 7800 read of 8690 input
        assert!(close(cache_read_ratio(cc), Some(7800.0 / 8690.0)));
    }

    #[test]
    fn spend_columns_add_up_to_window_totals() {
        let store = panel_fixture();
        let tz = FixedOffset::west_opt(4 * 3600).unwrap();
        for (w, tool) in scopes() {
            let since = w.since_ms(NOW);
            let sp = spend(&store.conn, w, NOW, tool, &tz).unwrap();
            assert_eq!(sp.by_month, w == Window::All);
            if w == Window::FourWeeks {
                assert_eq!(sp.periods.len(), 4);
            }
            if w == Window::TwelveWeeks {
                assert_eq!(sp.periods.len(), 12);
            }
            let t = totals(&store.conn, since, tool).unwrap();
            for (i, name) in sp.tools.iter().enumerate() {
                let (mut cost, mut unpriced) = (None::<f64>, 0);
                for p in &sp.periods {
                    if let Some(c) = p.cells[i].0 {
                        *cost.get_or_insert(0.0) += c;
                    }
                    unpriced += p.cells[i].1;
                }
                let want = t.by_tool.iter().find(|r| &r.tool == name);
                assert!(
                    close(cost, want.and_then(|r| r.tokens.estimated_cost_usd)),
                    "{w:?} {tool:?} {name}"
                );
                assert_eq!(unpriced, want.map_or(0, |r| r.tokens.unpriced_requests));
            }
        }
        // All time runs from the Antigravity month to now, gaps filled.
        let sp = spend(&store.conn, Window::All, NOW, None, &tz).unwrap();
        assert_eq!(
            sp.periods
                .iter()
                .map(|p| p.label.as_str())
                .collect::<Vec<_>>(),
            vec!["Aug 2026", "Sep 2026", "Oct 2026"]
        );
        assert_eq!(sp.tools, vec!["claude-code", "codex", "antigravity"]);
        // The latest week holds the requests of the last seven days.
        let sp = spend(&store.conn, Window::FourWeeks, NOW, Some("codex"), &tz).unwrap();
        assert!(close(sp.periods[3].cells[0].0, Some(1.5 + 0.2 + 0.01)));
        assert_eq!(sp.periods[3].cells[0].1, 1);
    }
}
