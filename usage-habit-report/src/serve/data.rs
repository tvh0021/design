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

#[cfg(test)]
mod tests {
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
}
