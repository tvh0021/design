//! What a subscription buys: how much usage fills one limit window, and how
//! the estimated API cost of a month compares with the subscription price.
//!
//! Limit readings come from `limit_samples`. Codex reports the used share of
//! both windows with every token count, so a window's fill is read as a run
//! of rising readings: from x % to y %, with the requests between them, then
//! scaled to 100 %. A drop in the share, a changed reset time, or a gap longer
//! than the window starts a new run, which is how a reset or a reset credit
//! in the middle of a session is kept out of the figure. Claude Code only
//! records a rejection, so a run there spans from the window's start (its
//! reset time minus the window length) to the first refusal: exactly 0 % to
//! 100 %. Antigravity records nothing, and is reported by month only.
//!
//! Nothing here reads prompt text; the only inputs are timestamps, shares,
//! and token counts.

use crate::query::{Tokens, rfc3339};
use anyhow::Result;
use chrono::{Datelike, TimeZone};
use rusqlite::{Connection, params};
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};

/// A run must rise at least this much to be used; smaller ones multiply
/// sampling noise too far.
pub const MIN_RISE_PERCENT: f64 = 20.0;
/// How many of the largest rises feed the average.
pub const RUNS_AVERAGED: usize = 10;

pub const SESSION_WINDOW_MS: i64 = 5 * 3600 * 1000;
pub const WEEKLY_WINDOW_MS: i64 = 7 * 24 * 3600 * 1000;

/// Monthly price and name of the plan behind each tool.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Subscription {
    pub name: String,
    pub monthly_usd: Option<f64>,
}

/// Built-in plans, overridable through `[subscriptions]` in config.toml.
pub fn default_subscription(tool: &str) -> Subscription {
    let (name, price) = match tool {
        "claude-code" => ("Claude Team, premium seat", Some(150.0)),
        "codex" => ("ChatGPT Plus", Some(20.0)),
        "antigravity" => ("Google AI Pro", Some(19.99)),
        _ => ("unknown plan", None),
    };
    Subscription {
        name: name.to_owned(),
        monthly_usd: price,
    }
}

/// Usage scaled to one whole window (0 % to 100 %).
#[derive(Debug, Clone, PartialEq, Serialize, Default)]
pub struct Capacity {
    /// Every token: uncached input, cache read, cache write, output.
    pub tokens: f64,
    /// Uncached input plus output: the part that is not cache traffic.
    pub billable_tokens: f64,
    pub requests: f64,
    pub sessions: f64,
    pub estimated_cost_usd: Option<f64>,
}

impl Capacity {
    fn scaled(t: &RunUsage, factor: f64) -> Self {
        Capacity {
            tokens: t.tokens as f64 * factor,
            billable_tokens: t.billable_tokens as f64 * factor,
            requests: t.requests as f64 * factor,
            sessions: t.sessions as f64 * factor,
            estimated_cost_usd: t.estimated_cost_usd.map(|c| c * factor),
        }
    }

    fn mean(items: &[&Capacity]) -> Option<Self> {
        if items.is_empty() {
            return None;
        }
        let n = items.len() as f64;
        let priced: Vec<f64> = items.iter().filter_map(|c| c.estimated_cost_usd).collect();
        Some(Capacity {
            tokens: items.iter().map(|c| c.tokens).sum::<f64>() / n,
            billable_tokens: items.iter().map(|c| c.billable_tokens).sum::<f64>() / n,
            requests: items.iter().map(|c| c.requests).sum::<f64>() / n,
            sessions: items.iter().map(|c| c.sessions).sum::<f64>() / n,
            estimated_cost_usd: if priced.is_empty() {
                None
            } else {
                Some(priced.iter().sum::<f64>() / priced.len() as f64)
            },
        })
    }
}

/// Requests between two readings.
#[derive(Debug, Clone, Default)]
struct RunUsage {
    tokens: i64,
    billable_tokens: i64,
    requests: i64,
    sessions: i64,
    estimated_cost_usd: Option<f64>,
    unpriced_requests: i64,
}

/// One rise of a window's used share, with the requests that caused it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Run {
    pub start: String,
    pub end: String,
    pub from_percent: f64,
    pub to_percent: f64,
    pub rise_percent: f64,
    /// Readings in the run (1 for a Claude Code rejection).
    pub readings: i64,
    #[serde(flatten)]
    pub usage: Tokens,
    pub sessions: i64,
    /// `usage` scaled to a whole window.
    pub per_window: Capacity,
    #[serde(skip)]
    pub start_ms: i64,
    #[serde(skip)]
    pub end_ms: i64,
}

/// Mean capacity of the runs that started in one calendar month.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DriftPoint {
    /// "2026-06"
    pub month: String,
    pub runs: i64,
    pub per_window: Capacity,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct WindowEstimate {
    /// `session` or `weekly`.
    pub window: String,
    pub window_hours: i64,
    /// Readings of this window in the store.
    pub readings: i64,
    /// Runs rising at least `MIN_RISE_PERCENT`, in the store.
    pub qualifying_runs: i64,
    /// The largest rises, at most `RUNS_AVERAGED`, largest first.
    pub runs: Vec<Run>,
    /// Mean of `runs[].per_window`; null without a run.
    pub per_window: Option<Capacity>,
    /// Every qualifying run, by the month it started in.
    pub drift: Vec<DriftPoint>,
    /// When no run exists: the busiest whole window seen, which did not
    /// exhaust the limit, so the true capacity is at least this much.
    pub lower_bound: Option<Capacity>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MonthValue {
    /// "2026-06"
    pub month: String,
    pub complete: bool,
    #[serde(flatten)]
    pub usage: Tokens,
    /// Estimated API cost over the subscription price; null without either.
    pub multiple: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ToolValue {
    pub tool: String,
    pub subscription: Subscription,
    /// `readings` (Codex), `rejections` (Claude Code), or `none`.
    pub limit_source: String,
    pub windows: Vec<WindowEstimate>,
    pub months: Vec<MonthValue>,
    /// Complete months only (not the current one, not the first one seen).
    pub complete_months: i64,
    pub mean_monthly_cost_usd: Option<f64>,
    /// `mean_monthly_cost_usd` over the subscription price.
    pub mean_multiple: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ValueReport {
    pub generated_at: String,
    pub min_rise_percent: f64,
    pub runs_averaged: i64,
    pub tools: Vec<ToolValue>,
}

struct Sample {
    ts_ms: i64,
    used_percent: f64,
    resets_at_ms: Option<i64>,
    rejected: bool,
}

fn window_ms(window: &str) -> i64 {
    if window == "weekly" {
        WEEKLY_WINDOW_MS
    } else {
        SESSION_WINDOW_MS
    }
}

fn samples(conn: &Connection, tool: &str, window: &str) -> Result<Vec<Sample>> {
    let mut stmt = conn.prepare_cached(
        "SELECT ts_ms, used_percent, resets_at_ms, kind = 'rejected'
         FROM limit_samples WHERE tool = ?1 AND window = ?2 ORDER BY ts_ms",
    )?;
    let rows = stmt
        .query_map(params![tool, window], |r| {
            Ok(Sample {
                ts_ms: r.get(0)?,
                used_percent: r.get(1)?,
                resets_at_ms: r.get(2)?,
                rejected: r.get(3)?,
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(rows)
}

/// Requests of `tool` after `from_ms` up to and including `to_ms`.
fn usage_between(conn: &Connection, tool: &str, from_ms: i64, to_ms: i64) -> Result<RunUsage> {
    conn.query_row(
        "SELECT COUNT(*),
                COALESCE(SUM(input_tokens + cache_read_input_tokens + cache_creation_input_tokens + output_tokens), 0),
                COALESCE(SUM(input_tokens + output_tokens), 0),
                COUNT(DISTINCT session_id),
                SUM(cost_usd),
                SUM(cost_usd IS NULL)
         FROM requests WHERE tool = ?1 AND ts_ms > ?2 AND ts_ms <= ?3",
        params![tool, from_ms, to_ms],
        |r| {
            Ok(RunUsage {
                requests: r.get(0)?,
                tokens: r.get(1)?,
                billable_tokens: r.get(2)?,
                sessions: r.get(3)?,
                estimated_cost_usd: r.get(4)?,
                unpriced_requests: r.get::<_, Option<i64>>(5)?.unwrap_or(0),
            })
        },
    )
    .map_err(Into::into)
}

fn tokens_of(conn: &Connection, tool: &str, from_ms: i64, to_ms: i64) -> Result<Tokens> {
    conn.query_row(
        "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(cache_read_input_tokens), 0),
                COALESCE(SUM(cache_creation_input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                SUM(cost_usd), COALESCE(SUM(cost_usd IS NULL), 0)
         FROM requests WHERE tool = ?1 AND ts_ms > ?2 AND ts_ms <= ?3",
        params![tool, from_ms, to_ms],
        |r| {
            Ok(Tokens {
                requests: r.get(0)?,
                input_tokens: r.get(1)?,
                cache_read_input_tokens: r.get(2)?,
                cache_creation_input_tokens: r.get(3)?,
                output_tokens: r.get(4)?,
                estimated_cost_usd: r.get(5)?,
                unpriced_requests: r.get(6)?,
            })
        },
    )
    .map_err(Into::into)
}

/// (start, end, from %, to %, readings) of each monotonic run.
fn runs_of(samples: &[Sample], window_len_ms: i64) -> Vec<(i64, i64, f64, f64, i64)> {
    let mut runs = Vec::new();
    let mut cur: Option<(i64, i64, f64, f64, i64, Option<i64>)> = None;
    for s in samples {
        if s.rejected {
            // A rejection is a whole window on its own: start to first refusal.
            let Some(reset) = s.resets_at_ms else {
                continue;
            };
            let start = reset - window_len_ms;
            if start >= s.ts_ms || s.ts_ms >= reset {
                continue;
            }
            match runs.last() {
                Some(&(st, _, _, _, _)) if st == start => continue,
                _ => runs.push((start, s.ts_ms, 0.0, 100.0, 1)),
            }
            cur = None;
            continue;
        }
        let split = match cur {
            None => true,
            Some((_, end, _, last, _, reset)) => {
                s.used_percent < last - 0.5
                    || s.ts_ms - end > window_len_ms
                    || matches!((reset, s.resets_at_ms), (Some(a), Some(b)) if a != b)
            }
        };
        if split {
            if let Some((a, b, c, d, n, _)) = cur.take() {
                runs.push((a, b, c, d, n));
            }
            cur = Some((
                s.ts_ms,
                s.ts_ms,
                s.used_percent,
                s.used_percent,
                1,
                s.resets_at_ms,
            ));
        } else if let Some(c) = &mut cur {
            c.1 = s.ts_ms;
            c.3 = c.3.max(s.used_percent);
            c.4 += 1;
            if c.5.is_none() {
                c.5 = s.resets_at_ms;
            }
        }
    }
    if let Some((a, b, c, d, n, _)) = cur {
        runs.push((a, b, c, d, n));
    }
    runs
}

fn month_key(ms: i64, tz: &impl TimeZone) -> String {
    tz.timestamp_millis_opt(ms)
        .single()
        .map(|t| format!("{:04}-{:02}", t.year(), t.month()))
        .unwrap_or_default()
}

fn window_estimate(
    conn: &Connection,
    tool: &str,
    window: &str,
    tz: &impl TimeZone,
) -> Result<WindowEstimate> {
    let len = window_ms(window);
    let samples = samples(conn, tool, window)?;
    let mut runs = Vec::new();
    for (start, end, from, to, readings) in runs_of(&samples, len) {
        let rise = to - from;
        if rise < MIN_RISE_PERCENT {
            continue;
        }
        let u = usage_between(conn, tool, start, end)?;
        let usage = Tokens {
            requests: u.requests,
            estimated_cost_usd: u.estimated_cost_usd,
            unpriced_requests: u.unpriced_requests,
            ..tokens_of(conn, tool, start, end)?
        };
        runs.push(Run {
            start: rfc3339(start),
            end: rfc3339(end),
            from_percent: from,
            to_percent: to,
            rise_percent: rise,
            readings,
            usage,
            sessions: u.sessions,
            per_window: Capacity::scaled(&u, 100.0 / rise),
            start_ms: start,
            end_ms: end,
        });
    }
    let qualifying = runs.len() as i64;
    let mut by_month: BTreeMap<String, Vec<&Capacity>> = BTreeMap::new();
    for r in &runs {
        by_month
            .entry(month_key(r.start_ms, tz))
            .or_default()
            .push(&r.per_window);
    }
    let drift = by_month
        .iter()
        .filter_map(|(m, caps)| {
            Some(DriftPoint {
                month: m.clone(),
                runs: caps.len() as i64,
                per_window: Capacity::mean(caps)?,
            })
        })
        .collect();
    runs.sort_by(|a, b| {
        b.rise_percent
            .total_cmp(&a.rise_percent)
            .then(a.start_ms.cmp(&b.start_ms))
    });
    runs.truncate(RUNS_AVERAGED);
    let per_window = Capacity::mean(&runs.iter().map(|r| &r.per_window).collect::<Vec<_>>());
    let lower_bound = if runs.is_empty() {
        busiest_window(conn, tool, len)?
    } else {
        None
    };
    Ok(WindowEstimate {
        window: window.to_owned(),
        window_hours: len / 3_600_000,
        readings: samples.len() as i64,
        qualifying_runs: qualifying,
        runs,
        per_window,
        drift,
        lower_bound,
    })
}

/// The window of `len` with the most tokens, stepping by one hour. Requests
/// are first summed by hour so the sweep is over a few thousand buckets.
fn busiest_window(conn: &Connection, tool: &str, len: i64) -> Result<Option<Capacity>> {
    const HOUR: i64 = 3_600_000;
    let mut stmt = conn.prepare_cached(
        "SELECT ts_ms / 3600000, COUNT(*),
                SUM(input_tokens + cache_read_input_tokens + cache_creation_input_tokens + output_tokens),
                SUM(input_tokens + output_tokens), SUM(cost_usd)
         FROM requests WHERE tool = ?1 GROUP BY 1 ORDER BY 1",
    )?;
    let hours: Vec<(i64, i64, i64, i64, Option<f64>)> = stmt
        .query_map(params![tool], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        })?
        .collect::<rusqlite::Result<_>>()?;
    if hours.is_empty() {
        return Ok(None);
    }
    let span = len / HOUR;
    let mut best: Option<(i64, usize, usize)> = None;
    let mut lo = 0;
    for hi in 0..hours.len() {
        while hours[hi].0 - hours[lo].0 >= span {
            lo += 1;
        }
        let tokens: i64 = hours[lo..=hi].iter().map(|h| h.2).sum();
        if best.is_none_or(|b| tokens > b.0) {
            best = Some((tokens, lo, hi));
        }
    }
    let Some((_, lo, hi)) = best else {
        return Ok(None);
    };
    let (start, end) = (hours[lo].0 * HOUR - 1, hours[hi].0 * HOUR + HOUR);
    let sessions: i64 = conn.query_row(
        "SELECT COUNT(DISTINCT session_id) FROM requests WHERE tool = ?1 AND ts_ms > ?2 AND ts_ms <= ?3",
        params![tool, start, end],
        |r| r.get(0),
    )?;
    let slice = &hours[lo..=hi];
    let priced: Vec<f64> = slice.iter().filter_map(|h| h.4).collect();
    Ok(Some(Capacity {
        tokens: slice.iter().map(|h| h.2).sum::<i64>() as f64,
        billable_tokens: slice.iter().map(|h| h.3).sum::<i64>() as f64,
        requests: slice.iter().map(|h| h.1).sum::<i64>() as f64,
        sessions: sessions as f64,
        estimated_cost_usd: if priced.is_empty() {
            None
        } else {
            Some(priced.iter().sum())
        },
    }))
}

fn months(
    conn: &Connection,
    tool: &str,
    price: Option<f64>,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Result<Vec<MonthValue>> {
    let mut stmt = conn.prepare_cached(
        "SELECT ts_ms, input_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                output_tokens, cost_usd
         FROM requests WHERE tool = ?1",
    )?;
    let mut by_month: BTreeMap<String, Tokens> = BTreeMap::new();
    for row in stmt.query_map(params![tool], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, i64>(1)?,
            r.get::<_, i64>(2)?,
            r.get::<_, i64>(3)?,
            r.get::<_, i64>(4)?,
            r.get::<_, Option<f64>>(5)?,
        ))
    })? {
        let (ts, i, cr, cw, o, cost) = row?;
        let t = by_month.entry(month_key(ts, tz)).or_default();
        t.requests += 1;
        t.input_tokens += i;
        t.cache_read_input_tokens += cr;
        t.cache_creation_input_tokens += cw;
        t.output_tokens += o;
        match cost {
            Some(c) => *t.estimated_cost_usd.get_or_insert(0.0) += c,
            None => t.unpriced_requests += 1,
        }
    }
    let current = month_key(now_ms, tz);
    let first = by_month.keys().next().cloned();
    Ok(by_month
        .into_iter()
        .map(|(month, usage)| MonthValue {
            complete: month != current && Some(&month) != first.as_ref(),
            multiple: match (usage.estimated_cost_usd, price) {
                (Some(c), Some(p)) if p > 0.0 => Some(c / p),
                _ => None,
            },
            month,
            usage,
        })
        .collect())
}

/// The whole report, for every tool that has requests or readings.
pub fn report(
    conn: &Connection,
    subscriptions: &BTreeMap<String, Subscription>,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Result<ValueReport> {
    let mut tools: BTreeSet<String> = conn
        .prepare("SELECT DISTINCT tool FROM requests")?
        .query_map([], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    tools.extend(
        conn.prepare("SELECT DISTINCT tool FROM limit_samples")?
            .query_map([], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?,
    );
    // The known tools always appear, so a plan with no readings still shows
    // its monthly figures (or its absence of data).
    tools.extend(crate::serve::data::TOOLS.iter().map(|t| t.to_string()));
    let mut ordered: Vec<String> = tools.into_iter().collect();
    ordered.sort_by_key(|t| crate::serve::data::tool_order(t));
    let mut out = Vec::new();
    for tool in ordered {
        let subscription = subscriptions
            .get(&tool)
            .cloned()
            .unwrap_or_else(|| default_subscription(&tool));
        let limit_source: String = conn
            .query_row(
                "SELECT COALESCE(MAX(CASE kind WHEN 'rejected' THEN 'rejections' ELSE 'readings' END), 'none')
                 FROM limit_samples WHERE tool = ?1",
                params![tool],
                |r| r.get(0),
            )?;
        let windows = vec![
            window_estimate(conn, &tool, "session", tz)?,
            window_estimate(conn, &tool, "weekly", tz)?,
        ];
        let months = months(conn, &tool, subscription.monthly_usd, now_ms, tz)?;
        let complete_months = months.iter().filter(|m| m.complete).count();
        let priced: Vec<f64> = months
            .iter()
            .filter(|m| m.complete || complete_months == 0)
            .filter_map(|m| m.usage.estimated_cost_usd)
            .collect();
        let mean_monthly_cost_usd = if priced.is_empty() {
            None
        } else {
            Some(priced.iter().sum::<f64>() / priced.len() as f64)
        };
        let mean_multiple = match (mean_monthly_cost_usd, subscription.monthly_usd) {
            (Some(c), Some(p)) if p > 0.0 => Some(c / p),
            _ => None,
        };
        out.push(ToolValue {
            tool,
            subscription,
            limit_source,
            windows,
            months,
            complete_months: complete_months as i64,
            mean_monthly_cost_usd,
            mean_multiple,
        });
    }
    Ok(ValueReport {
        generated_at: rfc3339(now_ms),
        min_rise_percent: MIN_RISE_PERCENT,
        runs_averaged: RUNS_AVERAGED as i64,
        tools: out,
    })
}

fn m_tokens(n: f64) -> String {
    format!("{:.1}M", n / 1e6)
}

fn usd(c: Option<f64>) -> String {
    match c {
        Some(c) => format!("${c:.2}"),
        None => "-".into(),
    }
}

/// The plain-text report `query value` prints without `--json`.
pub fn render_text(r: &ValueReport) -> String {
    use std::fmt::Write;
    let mut s = String::new();
    let _ = writeln!(
        s,
        "Subscription value. Per-window figures scale the largest {} rises of at least {:.0} % to a whole window.",
        r.runs_averaged, r.min_rise_percent
    );
    for t in &r.tools {
        let _ = writeln!(s);
        let _ = writeln!(
            s,
            "{}: {} at {}/month; limit readings: {}",
            t.tool,
            t.subscription.name,
            usd(t.subscription.monthly_usd),
            t.limit_source
        );
        for w in &t.windows {
            let _ = write!(s, "  {} window ({} h): ", w.window, w.window_hours);
            match (&w.per_window, &w.lower_bound) {
                (Some(c), _) => {
                    let _ = writeln!(
                        s,
                        "{} tokens, {} uncached+output, {:.0} requests, {:.1} sessions, est. {} per full window, from {} of {} runs",
                        m_tokens(c.tokens),
                        m_tokens(c.billable_tokens),
                        c.requests,
                        c.sessions,
                        usd(c.estimated_cost_usd),
                        w.runs.len(),
                        w.qualifying_runs
                    );
                }
                (None, Some(b)) => {
                    let _ = writeln!(
                        s,
                        "never exhausted; busiest window held {} tokens, {:.0} requests, est. {} (a lower bound)",
                        m_tokens(b.tokens),
                        b.requests,
                        usd(b.estimated_cost_usd)
                    );
                }
                (None, None) => {
                    let _ = writeln!(s, "no data");
                }
            }
            for d in &w.drift {
                let _ = writeln!(
                    s,
                    "    {}: {} runs, {} tokens, est. {} per full window",
                    d.month,
                    d.runs,
                    m_tokens(d.per_window.tokens),
                    usd(d.per_window.estimated_cost_usd)
                );
            }
        }
        for m in &t.months {
            let _ = writeln!(
                s,
                "  {}{}: est. {} = {} of the price",
                m.month,
                if m.complete { "" } else { " (partial)" },
                usd(m.usage.estimated_cost_usd),
                match m.multiple {
                    Some(x) => format!("{x:.1}x"),
                    None => "-".into(),
                }
            );
        }
        if let (Some(c), Some(x)) = (t.mean_monthly_cost_usd, t.mean_multiple) {
            let _ = writeln!(
                s,
                "  mean over {}: est. {} = {:.1}x the price",
                if t.complete_months > 0 {
                    format!("{} complete months", t.complete_months)
                } else {
                    format!("{} partial months", t.months.len())
                },
                usd(Some(c)),
                x
            );
        }
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;

    const HOUR: i64 = 3_600_000;

    fn s(ts_ms: i64, pct: f64, reset: Option<i64>) -> Sample {
        Sample {
            ts_ms,
            used_percent: pct,
            resets_at_ms: reset,
            rejected: false,
        }
    }

    #[test]
    fn runs_split_on_drops_resets_and_gaps() {
        let r = Some(10 * HOUR);
        let samples = vec![
            s(0, 10.0, r),
            s(HOUR, 30.0, r),
            s(2 * HOUR, 55.0, r),
            // a reset credit: the share falls back
            s(3 * HOUR, 5.0, r),
            s(4 * HOUR, 40.0, r),
            // a new window: different reset time
            s(5 * HOUR, 41.0, Some(20 * HOUR)),
            s(6 * HOUR, 70.0, Some(20 * HOUR)),
            // a gap longer than the window
            s(20 * HOUR, 72.0, None),
            s(21 * HOUR, 90.0, None),
        ];
        assert_eq!(
            runs_of(&samples, SESSION_WINDOW_MS),
            vec![
                (0, 2 * HOUR, 10.0, 55.0, 3),
                (3 * HOUR, 4 * HOUR, 5.0, 40.0, 2),
                (5 * HOUR, 6 * HOUR, 41.0, 70.0, 2),
                (20 * HOUR, 21 * HOUR, 72.0, 90.0, 2),
            ]
        );
    }

    #[test]
    fn rejections_span_a_whole_window_once() {
        let reject = |ts_ms, reset| Sample {
            ts_ms,
            used_percent: 100.0,
            resets_at_ms: Some(reset),
            rejected: true,
        };
        let samples = vec![
            reject(4 * HOUR, 5 * HOUR),
            reject(4 * HOUR + 1, 5 * HOUR),
            // reset earlier than the refusal: inconsistent, dropped
            reject(9 * HOUR, 8 * HOUR),
            reject(12 * HOUR, 15 * HOUR),
        ];
        assert_eq!(
            runs_of(&samples, SESSION_WINDOW_MS),
            vec![
                (0, 4 * HOUR, 0.0, 100.0, 1),
                (10 * HOUR, 12 * HOUR, 0.0, 100.0, 1)
            ]
        );
    }

    fn fixture() -> crate::store::Store {
        let store = crate::store::Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch(
                "INSERT INTO sources (id, tool, path) VALUES (1, 'codex', '/l1'), (2, 'claude-code', '/l2');
                 INSERT INTO sessions (tool, session_id) VALUES ('codex', 'a'), ('codex', 'b'), ('claude-code', 'c'), ('antigravity', 'd');",
            )
            .unwrap();
        // Codex: readings 10 -> 60 over two hours in June, with 1,000 tokens
        // and $1 between them, then a reset credit and 0 -> 25 with 300 tokens.
        let june = 1_781_000_000_000i64; // 2026-06-09
        let mut sql = String::new();
        for (i, (ts, pct)) in [
            (0, 10.0),
            (HOUR, 35.0),
            (2 * HOUR, 60.0),
            (3 * HOUR, 0.0),
            (4 * HOUR, 25.0),
        ]
        .iter()
        .enumerate()
        {
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'session', {pct}, {}, 'reading');",
                june + ts,
                june + 5 * HOUR
            ));
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'weekly', {}, NULL, 'reading');",
                june + ts,
                i as f64 * 12.5
            ));
        }
        for (k, (ts, tok, cost, sid)) in [
            (1, 400, 0.4, "a"),
            (HOUR + 1, 600, 0.6, "b"),
            (3 * HOUR + 1, 300, 0.3, "a"),
        ]
        .iter()
        .enumerate()
        {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('codex', 'r{k}', '{sid}', 1, {}, 'm', {tok}, 0, {cost});",
                june + ts
            ));
        }
        // Claude Code: one rejection at 3 h into a window, 5,000 tokens in it.
        let oct = 1_792_000_000_000i64;
        sql.push_str(&format!(
            "INSERT INTO limit_samples VALUES (2, 'claude-code', 'c', {}, 'session', 100, {}, 'rejected');",
            oct + 3 * HOUR,
            oct + 5 * HOUR
        ));
        sql.push_str(&format!(
            "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, cache_read_input_tokens, output_tokens, cost_usd)
             VALUES ('claude-code', 'c1', 'c', 2, {}, 'm', 1000, 4000, 0, 2.0),
                    ('claude-code', 'c2', 'c', 2, {}, 'm', 10, 0, 0, NULL);",
            oct + HOUR,
            oct + 6 * HOUR
        ));
        sql.push_str(&format!(
            "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
             VALUES ('antigravity', 'g1', 'd', 2, {}, 'm', 7, 0, 3.0);",
            oct
        ));
        store.conn.execute_batch(&sql).unwrap();
        store
    }

    #[test]
    fn codex_runs_scale_to_a_whole_window_and_drift_by_month() {
        let store = fixture();
        let now = 1_792_500_000_000;
        let r = report(&store.conn, &BTreeMap::new(), now, &Utc).unwrap();
        assert_eq!(
            r.tools.iter().map(|t| t.tool.as_str()).collect::<Vec<_>>(),
            ["claude-code", "codex", "antigravity"]
        );
        let codex = &r.tools[1];
        assert_eq!(codex.limit_source, "readings");
        assert_eq!(codex.subscription.name, "ChatGPT Plus");
        let session = &codex.windows[0];
        assert_eq!(session.window, "session");
        assert_eq!(session.readings, 5);
        assert_eq!(session.qualifying_runs, 2);
        assert_eq!(session.runs.len(), 2);
        let first = &session.runs[0];
        assert_eq!(
            (first.from_percent, first.to_percent, first.readings),
            (10.0, 60.0, 3)
        );
        assert_eq!(first.usage.requests, 2);
        assert_eq!(first.sessions, 2);
        assert_eq!(first.per_window.tokens, 2000.0);
        assert_eq!(first.per_window.estimated_cost_usd, Some(2.0));
        assert_eq!(first.per_window.sessions, 4.0);
        let second = &session.runs[1];
        assert_eq!(second.per_window.tokens, 1200.0);
        assert_eq!(session.per_window.as_ref().unwrap().tokens, 1600.0);
        assert_eq!(session.drift.len(), 1);
        assert_eq!(session.drift[0].month, "2026-06");
        assert_eq!(session.drift[0].runs, 2);
        assert!(session.lower_bound.is_none());
        // Weekly: one run 0 -> 50 with all 1,300 tokens.
        let weekly = &codex.windows[1];
        assert_eq!(weekly.runs.len(), 1);
        assert_eq!(weekly.runs[0].per_window.tokens, 2600.0);
        // Months: June is the first and only month, so it counts as the pool.
        assert_eq!(codex.months.len(), 1);
        assert_eq!(codex.months[0].month, "2026-06");
        assert!(!codex.months[0].complete);
        let multiple = codex.months[0].multiple.unwrap();
        assert!((multiple - 1.3 / 20.0).abs() < 1e-9, "{multiple}");
        assert!((codex.mean_monthly_cost_usd.unwrap() - 1.3).abs() < 1e-9);
    }

    #[test]
    fn claude_rejections_measure_a_full_window_and_weekly_is_a_lower_bound() {
        let store = fixture();
        let r = report(&store.conn, &BTreeMap::new(), 1_792_500_000_000, &Utc).unwrap();
        let claude = &r.tools[0];
        assert_eq!(claude.limit_source, "rejections");
        let session = &claude.windows[0];
        assert_eq!(session.runs.len(), 1);
        let run = &session.runs[0];
        assert_eq!((run.from_percent, run.to_percent), (0.0, 100.0));
        assert_eq!(run.usage.requests, 1);
        assert_eq!(run.usage.cache_read_input_tokens, 4000);
        assert_eq!(run.per_window.tokens, 5000.0);
        assert_eq!(run.per_window.billable_tokens, 1000.0);
        assert_eq!(run.per_window.estimated_cost_usd, Some(2.0));
        let weekly = &claude.windows[1];
        assert!(weekly.runs.is_empty());
        let bound = weekly.lower_bound.as_ref().unwrap();
        assert_eq!(bound.tokens, 5010.0);
        assert_eq!(bound.requests, 2.0);
        assert_eq!(bound.estimated_cost_usd, Some(2.0));
        // Antigravity: no readings, months only.
        let anti = &r.tools[2];
        assert_eq!(anti.limit_source, "none");
        assert!(anti.windows[0].runs.is_empty());
        assert_eq!(anti.windows[0].lower_bound.as_ref().unwrap().tokens, 7.0);
        assert!((anti.months[0].multiple.unwrap() - 3.0 / 19.99).abs() < 1e-9);
    }

    #[test]
    fn text_report_names_each_window_and_month() {
        let store = fixture();
        let r = report(&store.conn, &BTreeMap::new(), 1_792_500_000_000, &Utc).unwrap();
        let text = render_text(&r);
        assert!(text.contains("codex: ChatGPT Plus at $20.00/month; limit readings: readings"));
        assert!(text.contains("session window (5 h): 0.0M tokens"));
        assert!(text.contains("never exhausted; busiest window held"));
        assert!(text.contains("2026-06 (partial): est. $1.30 = 0.1x of the price"));
        assert!(text.contains("antigravity: Google AI Pro at $19.99/month; limit readings: none"));
    }

    #[test]
    fn configured_subscriptions_replace_defaults() {
        let store = fixture();
        let mut subs = BTreeMap::new();
        subs.insert(
            "codex".to_owned(),
            Subscription {
                name: "ChatGPT Pro".into(),
                monthly_usd: Some(200.0),
            },
        );
        let r = report(&store.conn, &subs, 1_792_500_000_000, &Utc).unwrap();
        assert_eq!(r.tools[1].subscription.name, "ChatGPT Pro");
        let multiple = r.tools[1].months[0].multiple.unwrap();
        assert!((multiple - 1.3 / 200.0).abs() < 1e-9, "{multiple}");
    }
}
