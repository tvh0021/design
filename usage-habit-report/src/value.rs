//! What a subscription buys: how much usage fills one limit window, how that
//! has drifted, how much of each window is used, and how the estimated API
//! cost of a month compares with the subscription price.
//!
//! Limit readings come from `limit_samples` (written by the log scanners) and
//! `live_limits` (sampled from `ai-usage` by `limits record`). Codex reports
//! the used share of both windows with every token count, so a window's fill
//! is read as a run of rising readings: from x % to y %, with the requests
//! between them, then scaled to 100 %. A drop in the share, a changed reset
//! time, or a gap longer than the window starts a new run, which is how a
//! reset or a reset credit in the middle of a session is kept out of the
//! figure. A run ends at the first reading of its highest share, so requests
//! made while a window sits at 100 % (paid with credits, or refused) are not
//! counted as part of the window. Claude Code only records a rejection, so a
//! run there spans from the window's start (its reset time minus the window
//! length) to the first refusal: exactly 0 % to 100 %.
//!
//! Live readings can name a quota pool (Claude's Fable weekly pool,
//! Antigravity's Gemini or Argon pools). A pool's window counts only the
//! requests of the models it covers, matched by name.
//!
//! Nothing here reads prompt text; the only inputs are timestamps, shares,
//! model names, and token counts.

use crate::query::{Tokens, rfc3339};
use crate::rates::{Price, RatesFile, Usage};
use anyhow::Result;
use chrono::{Datelike, TimeZone};
use rusqlite::{Connection, params};
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

/// A run must rise at least this much to be used; smaller ones multiply
/// sampling noise too far.
pub const MIN_RISE_PERCENT: f64 = 20.0;
/// How many of the largest rises feed the average.
pub const RUNS_AVERAGED: usize = 10;
/// The recent stretch the monthly pace is measured over.
pub const PACE_DAYS: i64 = 28;

pub const SESSION_WINDOW_MS: i64 = 5 * 3600 * 1000;
pub const WEEKLY_WINDOW_MS: i64 = 7 * 24 * 3600 * 1000;
const DAY_MS: i64 = 24 * 3600 * 1000;
/// Mean length of a calendar month in days, for the pace.
const MONTH_DAYS: f64 = 30.44;
/// Reset times closer than this are the same window: providers jitter.
const RESET_TOLERANCE_MS: i64 = 60_000;
/// A fall of at least this much before the reset time is a mid-window reset.
const RESET_DROP_PERCENT: f64 = 5.0;
/// A window whose highest reading reaches this is exhausted.
const EXHAUSTED_PERCENT: f64 = 99.5;
/// Credits spent this close to a mid-window reset are counted as paying for it.
const RESET_CREDIT_SLACK_MS: i64 = 15 * 60 * 1000;
/// How far back an older, higher credit balance can reappear from a
/// concurrent session's reading.
const STALE_BALANCE_MS: i64 = 60 * 60 * 1000;
/// Drift endpoints need at least this many runs in their month.
pub const DRIFT_MIN_RUNS: i64 = 3;
/// A session-to-week share needs at least this many session windows.
pub const SHARE_MIN_WINDOWS: i64 = 3;
/// A window counts toward a model when the model has this share of its cost.
pub const MODEL_WINDOW_SHARE: f64 = 0.8;

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
    pub output_tokens: f64,
    /// The same requests priced at the tool's reference model, so months
    /// with different models compare on one price list.
    pub reference_cost_usd: Option<f64>,
}

fn mean_of(values: impl Iterator<Item = Option<f64>>) -> Option<f64> {
    let v: Vec<f64> = values.flatten().collect();
    (!v.is_empty()).then(|| v.iter().sum::<f64>() / v.len() as f64)
}

impl Capacity {
    fn mean(items: &[&Capacity]) -> Option<Self> {
        if items.is_empty() {
            return None;
        }
        let n = items.len() as f64;
        let avg = |f: fn(&Capacity) -> f64| items.iter().map(|c| f(c)).sum::<f64>() / n;
        Some(Capacity {
            tokens: avg(|c| c.tokens),
            billable_tokens: avg(|c| c.billable_tokens),
            requests: avg(|c| c.requests),
            sessions: avg(|c| c.sessions),
            estimated_cost_usd: mean_of(items.iter().map(|c| c.estimated_cost_usd)),
            output_tokens: avg(|c| c.output_tokens),
            reference_cost_usd: mean_of(items.iter().map(|c| c.reference_cost_usd)),
        })
    }
}

/// What a full window held at list prices when one model did most of the
/// work in it: the limit read model by model.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ModelWindow {
    pub model: String,
    /// Full windows where this model had at least `MODEL_WINDOW_SHARE` of
    /// the estimated cost, and every request was priced.
    pub windows: i64,
    pub median_cost_usd: f64,
    pub min_cost_usd: f64,
    pub max_cost_usd: f64,
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
    /// The model with the largest share of estimated cost in these runs.
    pub main_model: Option<String>,
    /// That model's share of the estimated cost, 0..1.
    pub main_model_share: Option<f64>,
}

/// Lowest, middle, and highest per-window figure among the averaged runs.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Spread {
    pub cost_min: Option<f64>,
    pub cost_median: Option<f64>,
    pub cost_max: Option<f64>,
    pub tokens_min: f64,
    pub tokens_median: f64,
    pub tokens_max: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct UtilizationMonth {
    pub month: String,
    pub windows: i64,
    pub exhausted: i64,
    pub cut_short: i64,
    pub mean_peak_percent: Option<f64>,
}

/// How much of each window was used, over closed windows that had any use.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Utilization {
    /// Closed windows with at least one reading or refusal.
    pub windows: i64,
    /// Windows whose highest reading reached 100 %, or that refused a request.
    pub exhausted: i64,
    /// Windows a mid-window reset ended before their reset time.
    pub cut_short: i64,
    /// Mean of the highest reading of each window that ran to its reset
    /// time. Null when only refusals are known, since those show exhausted
    /// windows and nothing else.
    pub mean_peak_percent: Option<f64>,
    /// Falls of the share, with a later reset time, before the window's
    /// reset time: a reset credit or a reset granted by the provider.
    pub mid_window_resets: i64,
    pub by_month: Vec<UtilizationMonth>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct WindowEstimate {
    /// `session` or `weekly`.
    pub window: String,
    pub window_hours: i64,
    /// Readings of this window in the store, logged and live.
    pub readings: i64,
    /// Runs rising at least `MIN_RISE_PERCENT`, in the store.
    pub qualifying_runs: i64,
    /// The largest rises, at most `RUNS_AVERAGED`, largest first.
    pub runs: Vec<Run>,
    /// Mean of `runs[].per_window`; null without a run.
    pub per_window: Option<Capacity>,
    /// Every qualifying run, by the month it started in.
    pub drift: Vec<DriftPoint>,
    /// Every qualifying run, by the model that did most of its work.
    pub by_model: Vec<ModelWindow>,
    /// When no run exists: the busiest whole window seen, which did not
    /// exhaust the limit, so the true capacity is at least this much.
    pub lower_bound: Option<Capacity>,
    /// A per-model quota pool ("Fable", "Gemini"); null for the plan's window.
    pub pool: Option<String>,
    /// Models a pool covers; empty for the plan's window (every model).
    pub models: Vec<String>,
    /// Of `readings`, how many were sampled live by `limits record`.
    pub live_readings: i64,
    pub first_reading: Option<String>,
    pub last_reading: Option<String>,
    pub spread: Option<Spread>,
    pub utilization: Option<Utilization>,
    /// The newest reading of this window, logged or live.
    pub latest: Option<Latest>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Latest {
    pub at: String,
    pub used_percent: f64,
    pub resets_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MonthValue {
    /// "2026-06"
    pub month: String,
    pub complete: bool,
    #[serde(flatten)]
    pub usage: Tokens,
    /// Plan-covered estimated cost over the subscription price; null without
    /// either. Requests paid with credits past the limit are left out.
    pub multiple: Option<f64>,
    /// Days of the month with history: from the later of the month's start
    /// and the first request, to the earlier of its end and now.
    pub days_covered: f64,
    /// A partial month's cost at its own pace over the whole month.
    pub projected_cost_usd: Option<f64>,
    /// Estimated cost of requests paid with credits past the limit (Codex).
    pub credit_funded_cost_usd: Option<f64>,
}

/// Estimated cost a month at the pace of the last `PACE_DAYS` days (or since
/// the first request, when history is shorter).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Pace {
    pub days: f64,
    /// Plan-covered estimated cost scaled to a 30.44-day month.
    pub monthly_cost_usd: Option<f64>,
    pub multiple: Option<f64>,
}

/// How much of a weekly window one full session window uses, read from
/// paired readings of the two: within each session window, the weekly
/// share's rise over the session share's rise.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SessionShare {
    /// The session window's pool; null for the plan's window.
    pub session_pool: Option<String>,
    /// The weekly window's pool; null for the plan's window.
    pub weekly_pool: Option<String>,
    /// Session windows that rose at least `MIN_RISE_PERCENT`.
    pub windows: i64,
    /// Weekly percent per 100 % of session, pooled over `windows`; null
    /// below `SHARE_MIN_WINDOWS`.
    pub weekly_percent: Option<f64>,
}

/// The Codex credit balance over time.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct CreditUse {
    pub first_at: String,
    pub last_at: String,
    pub first_balance: f64,
    pub last_balance: f64,
    /// Every fall in the balance.
    pub spent: f64,
    /// Spent while a window read 100 %: usage past the limit.
    pub spent_at_limit: f64,
    /// Spent within 15 minutes of a mid-window reset.
    pub spent_near_resets: f64,
    /// Every rise in the balance (top-ups and grants).
    pub added: f64,
    /// Requests made while a window read 100 % and the balance fell.
    pub credit_funded: Tokens,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ToolValue {
    pub tool: String,
    pub subscription: Subscription,
    /// `readings` (logged), `live` (sampled only), `rejections`, or `none`.
    pub limit_source: String,
    pub windows: Vec<WindowEstimate>,
    pub months: Vec<MonthValue>,
    /// Complete months only (not the current one, not the first one seen).
    pub complete_months: i64,
    pub mean_monthly_cost_usd: Option<f64>,
    /// `mean_monthly_cost_usd` over the subscription price.
    pub mean_multiple: Option<f64>,
    /// The model the reference cost is priced at: the tool's costliest model
    /// over the last `PACE_DAYS` days.
    pub reference_model: Option<String>,
    pub first_request: Option<String>,
    pub last_request: Option<String>,
    pub pace: Option<Pace>,
    pub credits: Option<CreditUse>,
    /// One per session window paired with a weekly window it counts toward.
    pub session_shares: Vec<SessionShare>,
}

/// All plans together, at their recent pace.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Combined {
    /// Tools with a price.
    pub plans: i64,
    pub monthly_usd: f64,
    pub pace_monthly_cost_usd: Option<f64>,
    pub pace_multiple: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ValueReport {
    pub generated_at: String,
    pub min_rise_percent: f64,
    pub runs_averaged: i64,
    pub tools: Vec<ToolValue>,
    pub pace_days: i64,
    pub combined: Combined,
}

// ---------------------------------------------------------------------------
// Inputs

struct Sample {
    ts_ms: i64,
    used_percent: f64,
    resets_at_ms: Option<i64>,
    rejected: bool,
}

/// One request, reduced to what the report needs.
struct Req {
    ts: i64,
    session: u32,
    model: u32,
    input: i64,
    cache_read: i64,
    cache_write: i64,
    output: i64,
    cost: Option<f64>,
    reference_cost: Option<f64>,
}

/// A tool's requests in time order, with interned session and model names.
struct Data {
    reqs: Vec<Req>,
    models: Vec<String>,
}

impl Data {
    fn load(conn: &Connection, tool: &str) -> Result<Self> {
        let mut stmt = conn.prepare_cached(
            "SELECT ts_ms, session_id, COALESCE(canonical_model, model, ''), input_tokens,
                    cache_read_input_tokens, cache_creation_input_tokens, output_tokens, cost_usd
             FROM requests WHERE tool = ?1 ORDER BY ts_ms",
        )?;
        let mut sessions: HashMap<String, u32> = HashMap::new();
        let mut models: Vec<String> = Vec::new();
        let mut model_ids: HashMap<String, u32> = HashMap::new();
        let mut reqs = Vec::new();
        let mut rows = stmt.query(params![tool])?;
        while let Some(r) = rows.next()? {
            let sid: String = r.get(1)?;
            let n = sessions.len() as u32;
            let session = *sessions.entry(sid).or_insert(n);
            let m: String = r.get(2)?;
            let model = match model_ids.get(&m) {
                Some(&id) => id,
                None => {
                    let id = models.len() as u32;
                    models.push(m.clone());
                    model_ids.insert(m, id);
                    id
                }
            };
            reqs.push(Req {
                ts: r.get(0)?,
                session,
                model,
                input: r.get(3)?,
                cache_read: r.get(4)?,
                cache_write: r.get(5)?,
                output: r.get(6)?,
                cost: r.get(7)?,
                reference_cost: None,
            });
        }
        Ok(Data { reqs, models })
    }

    /// Requests after `from` up to and including `to`.
    fn between(&self, from: i64, to: i64) -> &[Req] {
        let lo = self.reqs.partition_point(|r| r.ts <= from);
        let hi = self.reqs.partition_point(|r| r.ts <= to);
        &self.reqs[lo..hi.max(lo)]
    }

    /// The costliest model over the last `PACE_DAYS` days, else all time.
    fn reference_model(&self, now_ms: i64) -> Option<u32> {
        let costliest = |reqs: &[Req]| {
            let mut by: HashMap<u32, f64> = HashMap::new();
            for r in reqs {
                *by.entry(r.model).or_default() += r.cost.unwrap_or(0.0);
            }
            by.into_iter()
                .filter(|(_, c)| *c > 0.0)
                .max_by(|a, b| a.1.total_cmp(&b.1).then(b.0.cmp(&a.0)))
                .map(|(m, _)| m)
        };
        costliest(self.between(now_ms - PACE_DAYS * DAY_MS, now_ms))
            .or_else(|| costliest(&self.reqs))
    }

    fn price_at(&mut self, price: &Price) {
        for r in &mut self.reqs {
            r.reference_cost = Some(price.cost(Usage {
                input: r.input,
                cache_read: r.cache_read,
                cache_creation: r.cache_write,
                output: r.output,
            }));
        }
    }
}

/// Sums over a set of requests.
#[derive(Default)]
struct Acc {
    t: Tokens,
    reference: Option<f64>,
    sessions: HashSet<u32>,
    /// model -> (requests, estimated cost)
    by_model: HashMap<u32, (i64, f64)>,
}

impl Acc {
    fn add(&mut self, r: &Req) {
        self.t.requests += 1;
        self.t.input_tokens += r.input;
        self.t.cache_read_input_tokens += r.cache_read;
        self.t.cache_creation_input_tokens += r.cache_write;
        self.t.output_tokens += r.output;
        match r.cost {
            Some(c) => *self.t.estimated_cost_usd.get_or_insert(0.0) += c,
            None => self.t.unpriced_requests += 1,
        }
        if let Some(c) = r.reference_cost {
            *self.reference.get_or_insert(0.0) += c;
        }
        self.sessions.insert(r.session);
        let m = self.by_model.entry(r.model).or_default();
        m.0 += 1;
        m.1 += r.cost.unwrap_or(0.0);
    }

    fn of<'a>(reqs: impl IntoIterator<Item = &'a Req>, scope: &Scope) -> Self {
        let mut a = Acc::default();
        for r in reqs {
            if scope.covers(r) {
                a.add(r);
            }
        }
        a
    }

    fn total_tokens(&self) -> i64 {
        self.t.input_tokens
            + self.t.cache_read_input_tokens
            + self.t.cache_creation_input_tokens
            + self.t.output_tokens
    }

    fn capacity(&self, factor: f64) -> Capacity {
        Capacity {
            tokens: self.total_tokens() as f64 * factor,
            billable_tokens: (self.t.input_tokens + self.t.output_tokens) as f64 * factor,
            requests: self.t.requests as f64 * factor,
            sessions: self.sessions.len() as f64 * factor,
            estimated_cost_usd: self.t.estimated_cost_usd.map(|c| c * factor),
            output_tokens: self.t.output_tokens as f64 * factor,
            reference_cost_usd: self.reference.map(|c| c * factor),
        }
    }
}

/// Which requests a window's limit covers: every model, or a pool's models.
struct Scope {
    models: Option<BTreeSet<u32>>,
}

impl Scope {
    fn covers(&self, r: &Req) -> bool {
        self.models.as_ref().is_none_or(|m| m.contains(&r.model))
    }
}

fn pool_words(pool: &str) -> Vec<String> {
    pool.to_ascii_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|w| !w.is_empty() && *w != "and")
        .map(str::to_owned)
        .collect()
}

/// Models whose name mentions one of `pool`'s words, when no other pool's
/// word appears later in the name: "gemini-4-argon" belongs to "Argon", not
/// "Gemini".
fn pool_models(pool: &str, pools: &[String], models: &[String]) -> BTreeSet<u32> {
    let last_hit = |p: &str, m: &str| {
        pool_words(p)
            .iter()
            .filter_map(|w| m.find(w.as_str()))
            .max()
    };
    let mut out = BTreeSet::new();
    for (i, m) in models.iter().enumerate() {
        let m = m.to_ascii_lowercase();
        let best = pools
            .iter()
            .filter_map(|p| Some((last_hit(p, &m)?, p)))
            .max_by_key(|(pos, _)| *pos);
        if best.is_some_and(|(_, p)| p == pool) {
            out.insert(i as u32);
        }
    }
    out
}

fn window_ms(window: &str) -> i64 {
    if window == "weekly" {
        WEEKLY_WINDOW_MS
    } else {
        SESSION_WINDOW_MS
    }
}

/// Logged and live readings of one window, in time order, with the live count.
fn samples(conn: &Connection, tool: &str, window: &str, pool: &str) -> Result<(Vec<Sample>, i64)> {
    let mut stmt = conn.prepare_cached(
        "SELECT ts_ms, used_percent, resets_at_ms, kind = 'rejected', 0 FROM limit_samples
             WHERE tool = ?1 AND window = ?2 AND ?3 = ''
         UNION ALL
         SELECT ts_ms, used_percent, resets_at_ms, 0, 1 FROM live_limits
             WHERE tool = ?1 AND window = ?2 AND pool = ?3
         ORDER BY 1, 4",
    )?;
    let mut live = 0;
    let mut out = Vec::new();
    let mut rows = stmt.query(params![tool, window, pool])?;
    while let Some(r) = rows.next()? {
        live += r.get::<_, i64>(4)?;
        out.push(Sample {
            ts_ms: r.get(0)?,
            used_percent: r.get(1)?,
            resets_at_ms: r.get(2)?,
            rejected: r.get(3)?,
        });
    }
    Ok((out, live))
}

// ---------------------------------------------------------------------------
// Runs, utilization, credits

fn same_reset(a: Option<i64>, b: Option<i64>) -> bool {
    match (a, b) {
        (Some(a), Some(b)) => (a - b).abs() <= RESET_TOLERANCE_MS,
        _ => true,
    }
}

/// (start, end, from %, to %, readings) of each monotonic run. A run ends at
/// the first reading of its highest share.
fn runs_of(samples: &[Sample], window_len_ms: i64) -> Vec<(i64, i64, f64, f64, i64)> {
    struct Cur {
        start: i64,
        end: i64,
        from: f64,
        max: f64,
        n: i64,
        reset: Option<i64>,
        last_ts: i64,
    }
    let mut runs = Vec::new();
    let mut cur: Option<Cur> = None;
    let close = |c: Cur, runs: &mut Vec<_>| runs.push((c.start, c.end, c.from, c.max, c.n));
    for s in samples {
        if s.rejected {
            // A refusal inside a run of live readings of the same window is
            // its last reading, at 100 %.
            if let Some(c) = cur.as_mut().filter(|c| {
                c.reset.is_some()
                    && same_reset(c.reset, s.resets_at_ms)
                    && s.ts_ms - c.last_ts <= window_len_ms
            }) {
                c.n += 1;
                c.last_ts = s.ts_ms;
                if c.max < 100.0 {
                    c.max = 100.0;
                    c.end = s.ts_ms;
                }
                continue;
            }
            // Otherwise a refusal is a whole window on its own: start to first refusal.
            let Some(reset) = s.resets_at_ms else {
                continue;
            };
            let start = reset - window_len_ms;
            if start >= s.ts_ms || s.ts_ms >= reset {
                continue;
            }
            if let Some(c) = cur.take() {
                close(c, &mut runs);
            }
            match runs.last() {
                Some(&(st, ..)) if (st - start).abs() <= RESET_TOLERANCE_MS => {}
                _ => runs.push((start, s.ts_ms, 0.0, 100.0, 1)),
            }
            continue;
        }
        // Concurrent sessions interleave their readings, so a lower share
        // against the same reset time, or an earlier reset time, is a stale
        // reading rather than a reset.
        if let Some(c) = &cur
            && s.ts_ms - c.last_ts <= window_len_ms
            && stale(c.reset, c.max, s)
        {
            continue;
        }
        let split = match &cur {
            None => true,
            Some(c) => {
                s.used_percent < c.max - 0.5
                    || s.ts_ms - c.last_ts > window_len_ms
                    // One window never lasts longer than its length, even
                    // when readings carry no reset time to split on.
                    || s.ts_ms - c.start > window_len_ms + RESET_TOLERANCE_MS
                    || !same_reset(c.reset, s.resets_at_ms)
            }
        };
        if split {
            if let Some(c) = cur.take() {
                close(c, &mut runs);
            }
            cur = Some(Cur {
                start: s.ts_ms,
                end: s.ts_ms,
                from: s.used_percent,
                max: s.used_percent,
                n: 1,
                reset: s.resets_at_ms,
                last_ts: s.ts_ms,
            });
        } else if let Some(c) = &mut cur {
            c.n += 1;
            c.last_ts = s.ts_ms;
            if s.used_percent > c.max {
                c.max = s.used_percent;
                c.end = s.ts_ms;
            }
            if c.reset.is_none() {
                c.reset = s.resets_at_ms;
            }
        }
    }
    if let Some(c) = cur {
        close(c, &mut runs);
    }
    runs
}

/// Whether a reading is older than the window state `reset`/`max` it follows:
/// a lower share against the same reset time, or an earlier reset time.
fn stale(reset: Option<i64>, max: f64, s: &Sample) -> bool {
    match (reset, s.resets_at_ms) {
        (Some(r), Some(sr)) => {
            sr < r - RESET_TOLERANCE_MS
                || ((sr - r).abs() <= RESET_TOLERANCE_MS && s.used_percent < max - 0.5)
        }
        _ => false,
    }
}

/// Timestamps of resets bought before a window's reset time: the share falls
/// and the reset time moves later. Stale readings from concurrent sessions
/// are skipped; a reading with no reset time cannot show a reset.
fn mid_window_resets(samples: &[Sample]) -> Vec<i64> {
    let mut out = Vec::new();
    let mut cur: Option<(i64, f64)> = None;
    for s in samples.iter().filter(|s| !s.rejected) {
        let Some(sr) = s.resets_at_ms else {
            continue;
        };
        match cur {
            Some((r, max)) if stale(Some(r), max, s) => {}
            Some((r, max)) if (sr - r).abs() <= RESET_TOLERANCE_MS => {
                cur = Some((r, max.max(s.used_percent)));
            }
            Some((r, max)) => {
                if s.ts_ms < r - RESET_TOLERANCE_MS && s.used_percent < max - RESET_DROP_PERCENT {
                    out.push(s.ts_ms);
                }
                cur = Some((sr, s.used_percent));
            }
            None => cur = Some((sr, s.used_percent)),
        }
    }
    out
}

/// One window instance: its reset time, peak reading, whether it refused,
/// and whether a reset ended it early.
type WindowPeak = (i64, Option<f64>, bool, bool);

/// Spans of time, as (start, end] in epoch ms.
type Spans = Vec<(i64, i64)>;

fn utilization(
    samples: &[Sample],
    window_len_ms: i64,
    resets: &[i64],
    now_ms: i64,
    tz: &impl TimeZone,
) -> Option<Utilization> {
    // One entry per window instance, keyed by its reset time: (peak reading, refused).
    let mut by_reset: Vec<WindowPeak> = Vec::new();
    let mut keyed: Vec<&Sample> = samples
        .iter()
        .filter(|s| s.resets_at_ms.is_some())
        .collect();
    keyed.sort_by_key(|s| s.resets_at_ms);
    for s in keyed {
        let reset = s.resets_at_ms.unwrap_or_default();
        let reading = (!s.rejected).then_some(s.used_percent);
        match by_reset.last_mut() {
            Some(w) if (reset - w.0).abs() <= RESET_TOLERANCE_MS => {
                w.1 = match (w.1, reading) {
                    (Some(a), Some(b)) => Some(a.max(b)),
                    (a, b) => a.or(b),
                };
                w.2 |= s.rejected;
            }
            _ => by_reset.push((reset, reading, s.rejected, false)),
        }
    }
    by_reset.retain(|w| w.0 <= now_ms);
    for w in &mut by_reset {
        w.3 = resets
            .iter()
            .any(|&t| t > w.0 - window_len_ms + RESET_TOLERANCE_MS && t < w.0 - RESET_TOLERANCE_MS);
    }
    if by_reset.is_empty() {
        return None;
    }
    let exhausted = |w: &WindowPeak| w.2 || w.1.is_some_and(|p| p >= EXHAUSTED_PERCENT);
    let summarise = |ws: &[&WindowPeak]| {
        (
            ws.len() as i64,
            ws.iter().filter(|w| exhausted(w)).count() as i64,
            ws.iter().filter(|w| w.3).count() as i64,
            // A refused window peaked at 100 %, whatever it last read.
            mean_of(
                ws.iter()
                    .filter(|w| !w.3)
                    .map(|w| if w.2 { Some(100.0) } else { w.1 }),
            ),
        )
    };
    let all: Vec<_> = by_reset.iter().collect();
    let (windows, exhausted_n, cut_short, mean_peak) = summarise(&all);
    let mut months: BTreeMap<String, Vec<&WindowPeak>> = BTreeMap::new();
    for w in &by_reset {
        months
            .entry(month_key(w.0 - window_len_ms, tz))
            .or_default()
            .push(w);
    }
    Some(Utilization {
        windows,
        exhausted: exhausted_n,
        cut_short,
        mean_peak_percent: mean_peak,
        mid_window_resets: resets.len() as i64,
        by_month: months
            .into_iter()
            .map(|(month, ws)| {
                let (windows, exhausted, cut_short, mean_peak_percent) = summarise(&ws);
                UtilizationMonth {
                    month,
                    windows,
                    exhausted,
                    cut_short,
                    mean_peak_percent,
                }
            })
            .collect(),
    })
}

/// Credit balance changes from the logged Codex readings, and the time spans
/// in which requests were paid with credits past the limit.
fn credits(
    conn: &Connection,
    tool: &str,
    resets: &[i64],
    data: &Data,
) -> Result<Option<(CreditUse, Spans)>> {
    let mut stmt = conn.prepare_cached(
        "SELECT ts_ms, MAX(CASE window WHEN 'session' THEN used_percent END),
                MAX(CASE window WHEN 'weekly' THEN used_percent END), MAX(credits_balance)
         FROM limit_samples WHERE tool = ?1 AND kind = 'reading' AND credits_balance IS NOT NULL
         GROUP BY ts_ms ORDER BY ts_ms",
    )?;
    let points: Vec<(i64, Option<f64>, Option<f64>, f64)> = stmt
        .query_map(params![tool], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
        })?
        .collect::<rusqlite::Result<_>>()?;
    let (Some(first), Some(last)) = (points.first(), points.last()) else {
        return Ok(None);
    };
    let mut use_ = CreditUse {
        first_at: rfc3339(first.0),
        last_at: rfc3339(last.0),
        first_balance: first.3,
        last_balance: last.3,
        spent: 0.0,
        spent_at_limit: 0.0,
        spent_near_resets: 0.0,
        added: 0.0,
        credit_funded: Tokens::default(),
    };
    // Concurrent sessions interleave readings, so an older, higher balance
    // can follow a newer one. The balance only rises when credits are added,
    // so a rise counts only when it tops every balance of the last hour.
    let mut spans = Vec::new();
    let mut level = first.3;
    let mut level_at = first.0;
    let mut level_pct = (first.1, first.2);
    for (i, b) in points.iter().enumerate().skip(1) {
        if b.3 > level + 1e-9 {
            let lookback = points[..i]
                .iter()
                .rev()
                .take_while(|p| b.0 - p.0 <= STALE_BALANCE_MS)
                .map(|p| p.3)
                .fold(f64::NEG_INFINITY, f64::max);
            if b.3 > lookback + 1e-9 {
                use_.added += b.3 - level;
                level = b.3;
                level_at = b.0;
                level_pct = (b.1, b.2);
            }
            continue;
        }
        if b.3 > level - 1e-9 {
            level_at = b.0;
            level_pct = (b.1, b.2);
            continue;
        }
        let spent = level - b.3;
        use_.spent += spent;
        // `resets` holds the first reading after each fall; the balance can
        // lag the share by a reading, hence the slack before the last level.
        // A reset bought at the limit also starts from 100 %, so the reset
        // test comes first.
        let near_reset = resets
            .iter()
            .any(|&r| r > level_at - RESET_CREDIT_SLACK_MS && r <= b.0);
        let at_limit = [level_pct.0, level_pct.1]
            .iter()
            .any(|p| p.is_some_and(|p| p >= EXHAUSTED_PERCENT));
        if near_reset {
            use_.spent_near_resets += spent;
        } else if at_limit {
            use_.spent_at_limit += spent;
            spans.push((level_at, b.0));
        }
        level = b.3;
        level_at = b.0;
        level_pct = (b.1, b.2);
    }
    use_.last_balance = level;
    let all = Scope { models: None };
    use_.credit_funded = Acc::of(spans.iter().flat_map(|&(a, b)| data.between(a, b)), &all).t;
    Ok(Some((use_, spans)))
}

fn in_spans(ts: i64, spans: &[(i64, i64)]) -> bool {
    let i = spans.partition_point(|s| s.1 < ts);
    spans.get(i).is_some_and(|s| ts > s.0 && ts <= s.1)
}

// ---------------------------------------------------------------------------
// Windows and months

fn month_key(ms: i64, tz: &impl TimeZone) -> String {
    tz.timestamp_millis_opt(ms)
        .single()
        .map(|t| format!("{:04}-{:02}", t.year(), t.month()))
        .unwrap_or_default()
}

/// Start and end (epoch ms) of the local calendar month "YYYY-MM".
fn month_bounds(key: &str, tz: &impl TimeZone) -> Option<(i64, i64)> {
    let (y, m) = key.split_once('-')?;
    let (y, m): (i32, u32) = (y.parse().ok()?, m.parse().ok()?);
    let (ny, nm) = if m == 12 { (y + 1, 1) } else { (y, m + 1) };
    let start = tz.with_ymd_and_hms(y, m, 1, 0, 0, 0).earliest()?;
    let end = tz.with_ymd_and_hms(ny, nm, 1, 0, 0, 0).earliest()?;
    Some((start.timestamp_millis(), end.timestamp_millis()))
}

fn median(sorted: &[f64]) -> f64 {
    let n = sorted.len();
    if n % 2 == 1 {
        sorted[n / 2]
    } else {
        (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0
    }
}

fn spread(runs: &[Run]) -> Option<Spread> {
    if runs.is_empty() {
        return None;
    }
    let mut tokens: Vec<f64> = runs.iter().map(|r| r.per_window.tokens).collect();
    tokens.sort_by(f64::total_cmp);
    let mut cost: Vec<f64> = runs
        .iter()
        .filter_map(|r| r.per_window.estimated_cost_usd)
        .collect();
    cost.sort_by(f64::total_cmp);
    Some(Spread {
        cost_min: cost.first().copied(),
        cost_median: (!cost.is_empty()).then(|| median(&cost)),
        cost_max: cost.last().copied(),
        tokens_min: tokens[0],
        tokens_median: median(&tokens),
        tokens_max: tokens[tokens.len() - 1],
    })
}

struct WindowSpec<'a> {
    window: &'a str,
    pool: Option<&'a str>,
    scope: Scope,
    models: Vec<String>,
}

fn window_estimate(
    conn: &Connection,
    tool: &str,
    spec: &WindowSpec,
    data: &Data,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Result<(WindowEstimate, Vec<i64>)> {
    let len = window_ms(spec.window);
    let (samples, live) = samples(conn, tool, spec.window, spec.pool.unwrap_or(""))?;
    let mut runs = Vec::new();
    let mut by_month: BTreeMap<String, (Vec<Capacity>, Acc)> = BTreeMap::new();
    let mut model_costs: BTreeMap<u32, Vec<f64>> = BTreeMap::new();
    for (start, end, from, to, readings) in runs_of(&samples, len) {
        let rise = to - from;
        if rise < MIN_RISE_PERCENT {
            continue;
        }
        let reqs = data.between(start, end);
        let acc = Acc::of(reqs, &spec.scope);
        // A rise with none of this tool's requests came from elsewhere (the
        // Claude plan's limits also cover claude.ai chat); it measures nothing.
        if acc.t.requests == 0 {
            continue;
        }
        let per_window = acc.capacity(100.0 / rise);
        let total: f64 = acc.by_model.values().map(|m| m.1).sum();
        if acc.t.unpriced_requests == 0
            && total > 0.0
            && let Some(cost) = per_window.estimated_cost_usd
            && let Some((&m, _)) = acc
                .by_model
                .iter()
                .find(|(_, v)| v.1 / total >= MODEL_WINDOW_SHARE)
        {
            model_costs.entry(m).or_default().push(cost);
        }
        let month = by_month.entry(month_key(start, tz)).or_default();
        month.0.push(per_window.clone());
        for r in reqs.iter().filter(|r| spec.scope.covers(r)) {
            month.1.add(r);
        }
        runs.push(Run {
            start: rfc3339(start),
            end: rfc3339(end),
            from_percent: from,
            to_percent: to,
            rise_percent: rise,
            readings,
            sessions: acc.sessions.len() as i64,
            usage: acc.t,
            per_window,
            start_ms: start,
            end_ms: end,
        });
    }
    let qualifying = runs.len() as i64;
    let drift = by_month
        .into_iter()
        .filter_map(|(month, (caps, acc))| {
            let per_window = Capacity::mean(&caps.iter().collect::<Vec<_>>())?;
            let total: f64 = acc.by_model.values().map(|m| m.1).sum();
            let main = acc
                .by_model
                .iter()
                .max_by(|a, b| a.1.1.total_cmp(&b.1.1).then(a.1.0.cmp(&b.1.0)));
            Some(DriftPoint {
                month,
                runs: caps.len() as i64,
                per_window,
                main_model: main.map(|(m, _)| data.models[*m as usize].clone()),
                main_model_share: main.filter(|_| total > 0.0).map(|(_, (_, c))| c / total),
            })
        })
        .collect();
    let mut by_model: Vec<ModelWindow> = model_costs
        .into_iter()
        .map(|(m, mut costs)| {
            costs.sort_by(f64::total_cmp);
            ModelWindow {
                model: data.models[m as usize].clone(),
                windows: costs.len() as i64,
                median_cost_usd: median(&costs),
                min_cost_usd: costs[0],
                max_cost_usd: costs[costs.len() - 1],
            }
        })
        .collect();
    by_model.sort_by(|a, b| b.windows.cmp(&a.windows).then(a.model.cmp(&b.model)));
    runs.sort_by(|a, b| {
        b.rise_percent
            .total_cmp(&a.rise_percent)
            .then(a.start_ms.cmp(&b.start_ms))
    });
    runs.truncate(RUNS_AVERAGED);
    let per_window = Capacity::mean(&runs.iter().map(|r| &r.per_window).collect::<Vec<_>>());
    let lower_bound = if runs.is_empty() {
        busiest_window(data, &spec.scope, len)
    } else {
        None
    };
    let resets = mid_window_resets(&samples);
    Ok((
        WindowEstimate {
            window: spec.window.to_owned(),
            window_hours: len / 3_600_000,
            readings: samples.len() as i64,
            qualifying_runs: qualifying,
            spread: spread(&runs),
            runs,
            per_window,
            drift,
            by_model,
            lower_bound,
            pool: spec.pool.map(str::to_owned),
            models: spec.models.clone(),
            live_readings: live,
            first_reading: samples.first().map(|s| rfc3339(s.ts_ms)),
            last_reading: samples.last().map(|s| rfc3339(s.ts_ms)),
            utilization: utilization(&samples, len, &resets, now_ms, tz),
            latest: samples.iter().rev().find(|s| !s.rejected).map(|s| Latest {
                at: rfc3339(s.ts_ms),
                used_percent: s.used_percent,
                resets_at: s.resets_at_ms.map(rfc3339),
            }),
        },
        resets,
    ))
}

/// The span of `len` with the most tokens, over request timestamps.
fn busiest_window(data: &Data, scope: &Scope, len: i64) -> Option<Capacity> {
    let reqs: Vec<&Req> = data.reqs.iter().filter(|r| scope.covers(r)).collect();
    let tokens = |r: &Req| r.input + r.cache_read + r.cache_write + r.output;
    let (mut lo, mut sum, mut best) = (0, 0i64, None::<(i64, usize, usize)>);
    for hi in 0..reqs.len() {
        sum += tokens(reqs[hi]);
        while reqs[hi].ts - reqs[lo].ts >= len {
            sum -= tokens(reqs[lo]);
            lo += 1;
        }
        if best.is_none_or(|b| sum > b.0) {
            best = Some((sum, lo, hi));
        }
    }
    let (_, lo, hi) = best?;
    let mut acc = Acc::default();
    for r in &reqs[lo..=hi] {
        acc.add(r);
    }
    Some(acc.capacity(1.0))
}

fn months(
    data: &Data,
    price: Option<f64>,
    funded: &[(i64, i64)],
    now_ms: i64,
    tz: &impl TimeZone,
) -> Vec<MonthValue> {
    let mut by_month: BTreeMap<String, (Acc, Option<f64>)> = BTreeMap::new();
    for r in &data.reqs {
        let m = by_month.entry(month_key(r.ts, tz)).or_default();
        m.0.add(r);
        if in_spans(r.ts, funded) {
            *m.1.get_or_insert(0.0) += r.cost.unwrap_or(0.0);
        }
    }
    let current = month_key(now_ms, tz);
    let first_key = by_month.keys().next().cloned();
    let first_ts = data.reqs.first().map_or(now_ms, |r| r.ts);
    by_month
        .into_iter()
        .map(|(month, (acc, funded_cost))| {
            let complete = month != current && Some(&month) != first_key.as_ref();
            let (days_covered, days) = match month_bounds(&month, tz) {
                Some((start, end)) => (
                    (end.min(now_ms) - start.max(first_ts)).max(0) as f64 / DAY_MS as f64,
                    (end - start) as f64 / DAY_MS as f64,
                ),
                None => (0.0, MONTH_DAYS),
            };
            let cost = acc.t.estimated_cost_usd;
            let covered = cost.map(|c| c - funded_cost.unwrap_or(0.0));
            MonthValue {
                complete,
                multiple: match (covered, price) {
                    (Some(c), Some(p)) if p > 0.0 => Some(c / p),
                    _ => None,
                },
                days_covered,
                projected_cost_usd: match cost {
                    Some(c) if !complete && days_covered >= 1.0 && days_covered < days - 0.01 => {
                        Some(c / days_covered * days)
                    }
                    _ => None,
                },
                credit_funded_cost_usd: funded_cost,
                month,
                usage: acc.t,
            }
        })
        .collect()
}

fn pace(data: &Data, price: Option<f64>, funded: &[(i64, i64)], now_ms: i64) -> Option<Pace> {
    let first = data.reqs.first()?.ts;
    let from = first.max(now_ms - PACE_DAYS * DAY_MS);
    let days = (now_ms - from) as f64 / DAY_MS as f64;
    if days < 2.0 {
        return None;
    }
    let mut cost = None::<f64>;
    for r in data.between(from - 1, now_ms) {
        if let Some(c) = r.cost.filter(|_| !in_spans(r.ts, funded)) {
            *cost.get_or_insert(0.0) += c;
        }
    }
    let monthly = cost.map(|c| c / days * MONTH_DAYS);
    Some(Pace {
        days,
        monthly_cost_usd: monthly,
        multiple: match (monthly, price) {
            (Some(c), Some(p)) if p > 0.0 => Some(c / p),
            _ => None,
        },
    })
}

/// The whole report: every known tool, plus any other with requests or readings.
pub fn report(
    conn: &Connection,
    subscriptions: &BTreeMap<String, Subscription>,
    rates: Option<&RatesFile>,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Result<ValueReport> {
    let mut tools: BTreeSet<String> = BTreeSet::new();
    for sql in [
        "SELECT DISTINCT tool FROM requests",
        "SELECT DISTINCT tool FROM limit_samples",
        "SELECT DISTINCT tool FROM live_limits",
    ] {
        tools.extend(
            conn.prepare(sql)?
                .query_map([], |r| r.get::<_, String>(0))?
                .collect::<rusqlite::Result<Vec<_>>>()?,
        );
    }
    // The known tools always appear, so a plan with no readings still shows
    // its monthly figures (or its absence of data).
    tools.extend(crate::serve::data::TOOLS.iter().map(|t| t.to_string()));
    let mut ordered: Vec<String> = tools.into_iter().collect();
    ordered.sort_by_key(|t| crate::serve::data::tool_order(t));
    let mut out = Vec::new();
    for tool in ordered {
        out.push(tool_value(conn, &tool, subscriptions, rates, now_ms, tz)?);
    }
    let priced: Vec<&ToolValue> = out
        .iter()
        .filter(|t| t.subscription.monthly_usd.is_some_and(|p| p > 0.0))
        .collect();
    let monthly_usd: f64 = priced
        .iter()
        .filter_map(|t| t.subscription.monthly_usd)
        .sum();
    let pace_cost: Option<f64> = priced
        .iter()
        .filter_map(|t| t.pace.as_ref()?.monthly_cost_usd)
        .fold(None, |a, c| Some(a.unwrap_or(0.0) + c));
    Ok(ValueReport {
        generated_at: rfc3339(now_ms),
        min_rise_percent: MIN_RISE_PERCENT,
        runs_averaged: RUNS_AVERAGED as i64,
        pace_days: PACE_DAYS,
        combined: Combined {
            plans: priced.len() as i64,
            monthly_usd,
            pace_monthly_cost_usd: pace_cost,
            pace_multiple: pace_cost
                .filter(|_| monthly_usd > 0.0)
                .map(|c| c / monthly_usd),
        },
        tools: out,
    })
}

fn tool_value(
    conn: &Connection,
    tool: &str,
    subscriptions: &BTreeMap<String, Subscription>,
    rates: Option<&RatesFile>,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Result<ToolValue> {
    let subscription = subscriptions
        .get(tool)
        .cloned()
        .unwrap_or_else(|| default_subscription(tool));
    let mut data = Data::load(conn, tool)?;
    let reference = data.reference_model(now_ms).and_then(|m| {
        let name = data.models[m as usize].clone();
        Some((name.clone(), rates?.lookup(&name).price?))
    });
    if let Some((_, price)) = &reference {
        data.price_at(price);
    }
    let limit_source: String = conn.query_row(
        "SELECT CASE
             WHEN EXISTS (SELECT 1 FROM limit_samples WHERE tool = ?1 AND kind = 'reading') THEN 'readings'
             WHEN EXISTS (SELECT 1 FROM live_limits WHERE tool = ?1) THEN 'live'
             WHEN EXISTS (SELECT 1 FROM limit_samples WHERE tool = ?1) THEN 'rejections'
             ELSE 'none' END",
        params![tool],
        |r| r.get(0),
    )?;
    let pools: Vec<(String, String)> = conn
        .prepare_cached(
            "SELECT DISTINCT pool, window FROM live_limits WHERE tool = ?1 AND pool != ''
             ORDER BY pool, window = 'weekly'",
        )?
        .query_map(params![tool], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let pool_names: Vec<String> = pools
        .iter()
        .map(|p| p.0.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let mut specs: Vec<WindowSpec> = ["session", "weekly"]
        .into_iter()
        .map(|w| WindowSpec {
            window: w,
            pool: None,
            scope: Scope { models: None },
            models: Vec::new(),
        })
        .collect();
    for (pool, window) in &pools {
        let ids = pool_models(pool, &pool_names, &data.models);
        specs.push(WindowSpec {
            window: if window == "weekly" {
                "weekly"
            } else {
                "session"
            },
            pool: Some(pool.as_str()),
            models: ids
                .iter()
                .map(|&i| data.models[i as usize].clone())
                .collect(),
            scope: Scope { models: Some(ids) },
        });
    }
    let mut windows = Vec::new();
    let mut resets = Vec::new();
    for spec in &specs {
        let (w, r) = window_estimate(conn, tool, spec, &data, now_ms, tz)?;
        if spec.pool.is_none() {
            resets.extend(r);
        }
        windows.push(w);
    }
    resets.sort_unstable();
    let session_shares = session_shares(conn, tool, &pools, &pool_names, &data)?;
    let (credits, funded) = match credits(conn, tool, &resets, &data)? {
        Some((c, spans)) => (Some(c), spans),
        None => (None, Vec::new()),
    };
    let months = months(&data, subscription.monthly_usd, &funded, now_ms, tz);
    let complete_months = months.iter().filter(|m| m.complete).count();
    let mean_monthly_cost_usd = mean_of(
        months
            .iter()
            .filter(|m| m.complete || complete_months == 0)
            .map(|m| m.usage.estimated_cost_usd),
    );
    let mean_multiple = mean_of(
        months
            .iter()
            .filter(|m| m.complete || complete_months == 0)
            .map(|m| m.multiple),
    );
    Ok(ToolValue {
        tool: tool.to_owned(),
        limit_source,
        windows,
        complete_months: complete_months as i64,
        mean_monthly_cost_usd,
        mean_multiple,
        reference_model: reference.map(|r| r.0),
        first_request: data.reqs.first().map(|r| rfc3339(r.ts)),
        last_request: data.reqs.last().map(|r| rfc3339(r.ts)),
        pace: pace(&data, subscription.monthly_usd, &funded, now_ms),
        credits,
        session_shares,
        months,
        subscription,
    })
}

/// Pair each session window with the weekly window it counts toward: the
/// weekly window of its own pool, or the plan's. A weekly pool with no
/// session window of its own (Claude's Fable) pairs with the plan's session,
/// which also fills with other models, so each window's session rise is cut
/// to the pool's share of that window's estimated cost: the figure is for a
/// session spent on the pool's models alone.
fn session_shares(
    conn: &Connection,
    tool: &str,
    pools: &[(String, String)],
    pool_names: &[String],
    data: &Data,
) -> Result<Vec<SessionShare>> {
    let has = |pool: &str, window: &str| pools.iter().any(|p| p.0 == pool && p.1 == window);
    let mut pairs = vec![(String::new(), String::new())];
    for (pool, window) in pools {
        if window == "session" {
            let weekly = if has(pool, "weekly") {
                pool.clone()
            } else {
                String::new()
            };
            pairs.push((pool.clone(), weekly));
        } else if !has(pool, "session") {
            pairs.push((String::new(), pool.clone()));
        }
    }
    let mut out = Vec::new();
    for (sp, wp) in pairs {
        let mut stmt = conn.prepare_cached(
            "SELECT s.ts_ms, s.used_percent, s.resets_at_ms, w.used_percent, w.resets_at_ms
                 FROM limit_samples s JOIN limit_samples w
                 ON w.tool = s.tool AND w.source_id = s.source_id AND w.ts_ms = s.ts_ms
                    AND w.window = 'weekly' AND w.kind = 'reading'
                 WHERE s.tool = ?1 AND s.window = 'session' AND s.kind = 'reading'
                    AND ?2 = '' AND ?3 = ''
             UNION ALL
             SELECT s.ts_ms, s.used_percent, s.resets_at_ms, w.used_percent, w.resets_at_ms
                 FROM live_limits s JOIN live_limits w
                 ON w.tool = s.tool AND w.ts_ms = s.ts_ms AND w.window = 'weekly' AND w.pool = ?3
                 WHERE s.tool = ?1 AND s.window = 'session' AND s.pool = ?2
             ORDER BY 1",
        )?;
        let rows: Vec<PairedReading> = stmt
            .query_map(params![tool, sp, wp], |r| {
                Ok(PairedReading {
                    ts: r.get(0)?,
                    session: r.get(1)?,
                    session_reset: r.get(2)?,
                    weekly: r.get(3)?,
                    weekly_reset: r.get(4)?,
                })
            })?
            .collect::<rusqlite::Result<_>>()?;
        if rows.is_empty() {
            continue;
        }
        let (windows, weekly_percent) = if sp.is_empty() && !wp.is_empty() {
            let scope = Scope {
                models: Some(pool_models(&wp, pool_names, &data.models)),
            };
            share_of(&rows, |start, end| {
                pool_cost_share(data, &scope, start, end)
            })
        } else {
            share_of(&rows, |_, _| Some(1.0))
        };
        out.push(SessionShare {
            session_pool: (!sp.is_empty()).then_some(sp),
            weekly_pool: (!wp.is_empty()).then_some(wp),
            windows,
            weekly_percent: weekly_percent.filter(|_| windows >= SHARE_MIN_WINDOWS),
        });
    }
    Ok(out)
}

/// The pool's share of the estimated cost of requests from `start` to
/// `end`; null when none of them is priced.
fn pool_cost_share(data: &Data, scope: &Scope, start: i64, end: i64) -> Option<f64> {
    let from = data.reqs.partition_point(|r| r.ts < start);
    let to = data.reqs.partition_point(|r| r.ts <= end);
    let (mut pool, mut all) = (0.0, 0.0);
    for r in &data.reqs[from..to] {
        let c = r.cost.unwrap_or(0.0);
        all += c;
        if scope.covers(r) {
            pool += c;
        }
    }
    (all > 0.0).then(|| pool / all)
}

/// A session reading and the weekly reading taken with it.
struct PairedReading {
    ts: i64,
    session: f64,
    session_reset: Option<i64>,
    weekly: f64,
    weekly_reset: Option<i64>,
}

/// (windows, weekly percent per full session) from time-ordered paired
/// readings. Readings are grouped by their two reset times, so a reset
/// bought mid-window starts a new group; the rise of each share is its
/// highest reading less its lowest, which also absorbs stale readings from
/// concurrent sessions. `weight(start, end)` scales a window's session rise
/// to the part spent on the weekly window's models; a null weight drops it.
fn share_of(
    rows: &[PairedReading],
    weight: impl Fn(i64, i64) -> Option<f64>,
) -> (i64, Option<f64>) {
    struct Group {
        session_reset: i64,
        weekly_reset: i64,
        session: (f64, f64),
        weekly: (f64, f64),
        first: i64,
        last: i64,
    }
    let mut groups: Vec<Group> = Vec::new();
    for r in rows {
        let (Some(sr), Some(wr)) = (r.session_reset, r.weekly_reset) else {
            continue;
        };
        // Concurrent sessions interleave, so look back over recent groups.
        let found = groups.iter_mut().rev().take(8).find(|g| {
            (g.session_reset - sr).abs() <= RESET_TOLERANCE_MS
                && (g.weekly_reset - wr).abs() <= RESET_TOLERANCE_MS
        });
        match found {
            Some(g) => {
                g.session = (g.session.0.min(r.session), g.session.1.max(r.session));
                g.weekly = (g.weekly.0.min(r.weekly), g.weekly.1.max(r.weekly));
                g.first = g.first.min(r.ts);
                g.last = g.last.max(r.ts);
            }
            None => groups.push(Group {
                session_reset: sr,
                weekly_reset: wr,
                session: (r.session, r.session),
                weekly: (r.weekly, r.weekly),
                first: r.ts,
                last: r.ts,
            }),
        }
    }
    let rises: Vec<(f64, f64)> = groups
        .iter()
        .map(|g| (g, g.session.1 - g.session.0))
        .filter(|(_, rise)| *rise >= MIN_RISE_PERCENT)
        .filter_map(|(g, rise)| Some((rise * weight(g.first, g.last)?, g.weekly.1 - g.weekly.0)))
        .collect();
    let session: f64 = rises.iter().map(|r| r.0).sum();
    let weekly: f64 = rises.iter().map(|r| r.1).sum();
    (
        rises.len() as i64,
        (session > 0.0).then(|| 100.0 * weekly / session),
    )
}

// ---------------------------------------------------------------------------
// Words

/// "12.3M" for millions, "4.5B" for billions, plain thousands below that.
pub fn short_tokens(n: f64) -> String {
    if n >= 1e9 {
        format!("{:.1}B", n / 1e9)
    } else if n >= 1e6 {
        format!("{:.1}M", n / 1e6)
    } else if n >= 1e4 {
        format!("{:.0}k", n / 1e3)
    } else {
        format!("{n:.0}")
    }
}

/// "$1,297" (whole dollars, grouped).
pub fn dollars(c: f64) -> String {
    let sign = if c.round() < 0.0 { "-" } else { "" };
    format!("{sign}${}", grouped(c.abs()))
}

/// "1,297": rounded to a whole number, grouped by thousands.
pub fn grouped(c: f64) -> String {
    let n = c.round() as i64;
    let digits = n.abs().to_string();
    let mut out = String::new();
    for (i, ch) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(ch);
    }
    format!("{}{out}", if n < 0 { "-" } else { "" })
}

/// "+84 %" / "-63 %".
fn change(a: f64, b: f64) -> String {
    if a.abs() < 1e-12 {
        return "n/a".into();
    }
    let p = (b / a - 1.0) * 100.0;
    format!("{}{:.0} %", if p >= 0.0 { "+" } else { "-" }, p.abs())
}

fn window_words(w: &WindowEstimate) -> &'static str {
    if w.window == "weekly" {
        "weekly"
    } else {
        "5-hour"
    }
}

fn capitalised(s: &str) -> String {
    let mut c = s.chars();
    c.next()
        .map_or(String::new(), |f| f.to_uppercase().chain(c).collect())
}

pub fn date_of(rfc: &str) -> &str {
    rfc.get(..10).unwrap_or(rfc)
}

/// Drift endpoints: the first and last month with enough runs.
pub fn drift_ends(w: &WindowEstimate) -> Option<(&DriftPoint, &DriftPoint)> {
    let solid: Vec<&DriftPoint> = w
        .drift
        .iter()
        .filter(|d| d.runs >= DRIFT_MIN_RUNS)
        .collect();
    match (solid.first(), solid.last()) {
        (Some(a), Some(b)) if a.month != b.month => Some((a, b)),
        _ => None,
    }
}

/// One sentence on how a window's capacity moved, or None without two solid months.
pub fn drift_sentence(t: &ToolValue, w: &WindowEstimate) -> Option<String> {
    let (a, b) = drift_ends(w)?;
    let (ca, cb) = (
        a.per_window.estimated_cost_usd?,
        b.per_window.estimated_cost_usd?,
    );
    let mut s = format!(
        "{} window, {} to {}: est. {} to {} ({}), tokens {} to {} ({}).",
        capitalised(window_words(w)),
        a.month,
        b.month,
        dollars(ca),
        dollars(cb),
        change(ca, cb),
        short_tokens(a.per_window.tokens),
        short_tokens(b.per_window.tokens),
        change(a.per_window.tokens, b.per_window.tokens)
    );
    if let (Some(model), Some(ra), Some(rb)) = (
        &t.reference_model,
        a.per_window.reference_cost_usd,
        b.per_window.reference_cost_usd,
    ) {
        let k = (rb / ra - 1.0) * 100.0;
        let verdict = if k.abs() <= 15.0 {
            format!(
                "so it holds about the same work and {} models moved its value",
                if cb < ca { "cheaper" } else { "pricier" }
            )
        } else if k < 0.0 {
            "so it holds less work".to_string()
        } else {
            "so it holds more work".to_string()
        };
        s.push_str(&format!(
            " At {model} rates throughout: {} to {} ({}), {verdict}.",
            dollars(ra),
            dollars(rb),
            change(ra, rb)
        ));
    }
    if let (Some(ma), Some(mb)) = (&a.main_model, &b.main_model)
        && ma != mb
    {
        s.push_str(&format!(" Main model: {ma}, then {mb}."));
    }
    Some(s)
}

pub fn main_window<'a>(t: &'a ToolValue, window: &str) -> Option<&'a WindowEstimate> {
    t.windows
        .iter()
        .find(|w| w.pool.is_none() && w.window == window)
}

/// What a finding is about, so the dashboard can lead with the headlines and
/// set the rest beside the tables they describe.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Finding {
    /// The combined pace, and each tool's capacity and pace.
    Headline,
    Drift,
    Limits,
    Credits,
    /// A per-model pool's capacity.
    Pool,
    /// A window at its limit at the last live reading.
    Now,
}

/// Plain-language findings, most general first. Shared by the text report
/// and the dashboard so both say the same thing.
pub fn findings(r: &ValueReport) -> Vec<String> {
    tagged_findings(r).into_iter().map(|(_, s)| s).collect()
}

/// `findings`, each with what it is about.
pub fn tagged_findings(r: &ValueReport) -> Vec<(Finding, String)> {
    let mut out = Vec::new();
    let c = &r.combined;
    if let (Some(cost), Some(x)) = (c.pace_monthly_cost_usd, c.pace_multiple) {
        let mut s = format!(
            "Over the last {} days, the same use at API list prices would cost est. {} a month. The {} plans cost {} a month, so that is {:.1}x their price.",
            r.pace_days,
            dollars(cost),
            c.plans,
            dollars(c.monthly_usd),
            x
        );
        for t in &r.tools {
            if let Some(p) = t
                .pace
                .as_ref()
                .filter(|p| p.days < r.pace_days as f64 - 1.0)
            {
                s.push_str(&format!(
                    " {} has only {:.0} days of history.",
                    t.tool, p.days
                ));
            }
        }
        out.push((Finding::Headline, s));
    }
    for t in &r.tools {
        let mut s = String::new();
        let session = main_window(t, "session");
        let weekly = main_window(t, "weekly");
        match session {
            Some(w) if w.per_window.is_some() => {
                let c = w.per_window.as_ref().unwrap();
                s.push_str(&format!(
                    "{}: a full 5-hour window holds est. {} ({} tokens, {} requests, {:.1} sessions",
                    t.tool,
                    c.estimated_cost_usd.map_or("-".into(), dollars),
                    short_tokens(c.tokens),
                    grouped(c.requests),
                    c.sessions,
                ));
                match w
                    .spread
                    .as_ref()
                    .and_then(|s| Some((s.cost_min?, s.cost_max?)))
                {
                    Some((lo, hi)) if w.runs.len() > 1 => {
                        s.push_str(&format!("; runs {} to {}).", dollars(lo), dollars(hi)))
                    }
                    _ => s.push_str(")."),
                }
            }
            Some(w) if w.lower_bound.is_some() => s.push_str(&format!(
                "{}: no 5-hour window seen full; the busiest held at least est. {}.",
                t.tool,
                w.lower_bound
                    .as_ref()
                    .and_then(|b| b.estimated_cost_usd)
                    .map_or("-".into(), dollars)
            )),
            _ => s.push_str(&format!("{}: no requests recorded.", t.tool)),
        }
        if let Some(w) = weekly {
            match (&w.per_window, &w.lower_bound) {
                (Some(c), _) => {
                    s.push_str(&format!(
                        " A full week holds est. {}",
                        c.estimated_cost_usd.map_or("-".into(), dollars),
                    ));
                    let ratio = session
                        .and_then(|s| s.per_window.as_ref()?.estimated_cost_usd)
                        .zip(c.estimated_cost_usd)
                        .filter(|(s, _)| *s > 0.0)
                        .map(|(s, w)| w / s);
                    match ratio {
                        Some(k) => s.push_str(&format!(", {k:.1} 5-hour windows.")),
                        None => s.push('.'),
                    }
                }
                (None, Some(b)) => s.push_str(&format!(
                    " No week seen full; the busiest held at least est. {}.",
                    b.estimated_cost_usd.map_or("-".into(), dollars)
                )),
                _ => {}
            }
        }
        if let (Some(p), Some(price)) = (&t.pace, t.subscription.monthly_usd)
            && let (Some(cost), Some(x)) = (p.monthly_cost_usd, p.multiple)
        {
            s.push_str(&format!(
                " Pace: est. {} a month, {:.1}x the {} plan",
                dollars(cost),
                x,
                dollars(price)
            ));
            if p.days < r.pace_days as f64 - 1.0 {
                s.push_str(&format!(" ({:.0} days of history)", p.days));
            }
            s.push('.');
        }
        out.push((Finding::Headline, s));
        for w in t.windows.iter().filter(|w| w.pool.is_none()) {
            if let Some(d) = drift_sentence(t, w) {
                out.push((Finding::Drift, format!("{} drift: {d}", t.tool)));
            }
        }
        if let Some(u) = utilization_sentence(t) {
            out.push((Finding::Limits, u));
        }
        if let Some(c) = &t.credits
            && c.spent > 0.5
        {
            let mut s = format!(
                "{} credits, {} to {}: {}",
                t.tool,
                date_of(&c.first_at),
                date_of(&c.last_at),
                if c.added > 0.5 {
                    format!("{} added, ", grouped(c.added))
                } else {
                    String::new()
                },
            );
            s.push_str(&format!(
                "{} spent, {} left.",
                grouped(c.spent),
                grouped(c.last_balance)
            ));
            if c.spent_at_limit > 0.5 {
                s.push_str(&format!(
                    " {} paid for {} requests at a full window (est. {}, left out of the plan's value).",
                    grouped(c.spent_at_limit),
                    grouped(c.credit_funded.requests as f64),
                    c.credit_funded.estimated_cost_usd.map_or("-".into(), dollars)
                ));
            }
            if c.spent_near_resets > 0.5 {
                s.push_str(&format!(
                    " {} paid for mid-window resets.",
                    grouped(c.spent_near_resets)
                ));
            }
            out.push((Finding::Credits, s));
        }
        for w in t.windows.iter().filter(|w| w.pool.is_some()) {
            let pool = w.pool.as_deref().unwrap_or("");
            if let Some(c) = &w.per_window {
                out.push((
                    Finding::Pool,
                    format!(
                        "{} {pool} pool holds est. {} of use in a full {} window, {} tokens.",
                        t.tool,
                        c.estimated_cost_usd.map_or("-".into(), dollars),
                        window_words(w),
                        short_tokens(c.tokens)
                    ),
                ));
            }
        }
        // Windows at their limit right now, from live readings.
        for w in &t.windows {
            if w.latest
                .as_ref()
                .is_some_and(|l| l.used_percent >= EXHAUSTED_PERCENT)
                && let Some(l) = latest_sentence(t, w)
            {
                out.push((Finding::Now, l));
            }
        }
    }
    out
}

pub fn utilization_sentence(t: &ToolValue) -> Option<String> {
    let session = main_window(t, "session").and_then(|w| w.utilization.as_ref());
    let weekly = main_window(t, "weekly").and_then(|w| w.utilization.as_ref());
    let mut s = String::new();
    // "33 reached 100 % and 11 were reset early; the 207 that ran their
    // course peaked at 44 % on average"
    let course = |u: &Utilization, what: &str| {
        let mut s = format!("{} of {} {what} hit 100 %", u.exhausted, u.windows);
        if u.cut_short > 0 {
            s.push_str(&format!(", {} reset early", u.cut_short));
        }
        if let Some(p) = u.mean_peak_percent {
            s.push_str(&format!("; the rest peaked at {p:.0} % on average"));
        }
        s
    };
    match session {
        Some(u) if u.mean_peak_percent.is_some() || u.cut_short > 0 => s.push_str(&format!(
            "{} limits: {}.",
            t.tool,
            course(u, "5-hour windows")
        )),
        Some(u) => {
            let w = main_window(t, "session")?;
            s.push_str(&format!(
                "{} logged {} full 5-hour windows from {} to {}; its logs record no others.",
                t.tool,
                u.exhausted,
                date_of(w.first_reading.as_deref().unwrap_or("")),
                date_of(w.last_reading.as_deref().unwrap_or(""))
            ));
        }
        None => return None,
    }
    if let Some(u) = weekly.filter(|u| u.mean_peak_percent.is_some()) {
        let mut c = course(u, "weekly windows");
        c[..1].make_ascii_uppercase();
        s.push_str(&format!(" {c}."));
    }
    Some(s)
}

/// "Argon pool weekly window: 100 % used at 2026-10-23, resets 2026-10-24."
fn latest_sentence(t: &ToolValue, w: &WindowEstimate) -> Option<String> {
    let l = w.latest.as_ref().filter(|_| w.live_readings > 0)?;
    let mut s = format!(
        "{} {}: {:.0} % used on {}",
        t.tool,
        window_label(w),
        l.used_percent,
        date_of(&l.at)
    );
    if let Some(r) = &l.resets_at {
        s.push_str(&format!(", resets {}", date_of(r)));
    }
    s.push('.');
    Some(s)
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

pub fn window_label(w: &WindowEstimate) -> String {
    match &w.pool {
        Some(p) => format!("{p} pool, {} window ({} h)", w.window, w.window_hours),
        None => format!("{} window ({} h)", w.window, w.window_hours),
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
    let _ = writeln!(s);
    for f in findings(r) {
        let _ = writeln!(s, "- {f}");
    }
    for t in &r.tools {
        let _ = writeln!(s);
        let _ = writeln!(
            s,
            "{}: {} at {}/month; limit readings: {}{}",
            t.tool,
            t.subscription.name,
            usd(t.subscription.monthly_usd),
            t.limit_source,
            match &t.reference_model {
                Some(m) => format!("; reference model {m}"),
                None => String::new(),
            }
        );
        for w in &t.windows {
            let _ = write!(s, "  {}: ", window_label(w));
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
                    if let Some(sp) = &w.spread
                        && let (Some(lo), Some(mid), Some(hi)) =
                            (sp.cost_min, sp.cost_median, sp.cost_max)
                    {
                        let _ = writeln!(
                            s,
                            "    runs range {} to {}, median {}",
                            usd(Some(lo)),
                            usd(Some(hi)),
                            usd(Some(mid))
                        );
                    }
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
                    "    {}: {} runs, {} tokens, est. {} per full window{}{}",
                    d.month,
                    d.runs,
                    m_tokens(d.per_window.tokens),
                    usd(d.per_window.estimated_cost_usd),
                    match d.per_window.reference_cost_usd {
                        Some(c) => format!(", {} at reference rates", usd(Some(c))),
                        None => String::new(),
                    },
                    match (&d.main_model, d.main_model_share) {
                        (Some(m), Some(x)) => format!(", mostly {m} ({:.0} %)", x * 100.0),
                        _ => String::new(),
                    }
                );
            }
            if let Some(u) = &w.utilization {
                let _ = writeln!(
                    s,
                    "    windows used {}, exhausted {}{}, mid-window resets {}",
                    u.windows,
                    u.exhausted,
                    match u.mean_peak_percent {
                        Some(p) => format!(", mean peak {p:.0} %"),
                        None => String::new(),
                    },
                    u.mid_window_resets
                );
            }
        }
        for m in &t.months {
            let _ = writeln!(
                s,
                "  {}{}: est. {} = {} of the price{}",
                m.month,
                if m.complete { "" } else { " (partial)" },
                usd(m.usage.estimated_cost_usd),
                match m.multiple {
                    Some(x) => format!("{x:.1}x"),
                    None => "-".into(),
                },
                match m.projected_cost_usd {
                    Some(c) => format!(", on pace for {}", usd(Some(c))),
                    None => String::new(),
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
        if let Some(p) = &t.pace
            && let (Some(c), Some(x)) = (p.monthly_cost_usd, p.multiple)
        {
            let _ = writeln!(
                s,
                "  pace over the last {:.0} days: est. {} a month = {:.1}x the price",
                p.days,
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
        let r = Some(5 * HOUR);
        let samples = vec![
            s(0, 10.0, r),
            s(HOUR, 30.0, r),
            // a concurrent session's older reading: skipped
            s(HOUR + 1, 20.0, r),
            s(2 * HOUR, 55.0, r),
            // a reset credit: the share falls back and the reset moves later
            s(3 * HOUR, 5.0, Some(8 * HOUR)),
            // an older reading from before the reset: skipped
            s(3 * HOUR + 1, 55.0, r),
            s(4 * HOUR, 40.0, Some(8 * HOUR)),
            // a new window: different reset time
            s(9 * HOUR, 41.0, Some(13 * HOUR)),
            s(10 * HOUR, 70.0, Some(13 * HOUR)),
            // a gap longer than the window
            s(20 * HOUR, 72.0, None),
            s(21 * HOUR, 90.0, None),
        ];
        assert_eq!(mid_window_resets(&samples), vec![3 * HOUR]);
        assert_eq!(
            runs_of(&samples, SESSION_WINDOW_MS),
            vec![
                (0, 2 * HOUR, 10.0, 55.0, 3),
                (3 * HOUR, 4 * HOUR, 5.0, 40.0, 2),
                (9 * HOUR, 10 * HOUR, 41.0, 70.0, 2),
                (20 * HOUR, 21 * HOUR, 72.0, 90.0, 2),
            ]
        );
    }

    #[test]
    fn runs_never_outlast_their_window() {
        let samples = vec![
            s(0, 10.0, None),
            s(2 * HOUR, 20.0, None),
            s(4 * HOUR, 30.0, None),
            s(6 * HOUR, 40.0, None),
            s(7 * HOUR, 50.0, None),
        ];
        assert_eq!(
            runs_of(&samples, SESSION_WINDOW_MS),
            vec![
                (0, 4 * HOUR, 10.0, 30.0, 3),
                (6 * HOUR, 7 * HOUR, 40.0, 50.0, 2)
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
            // The reset credit at 3 h starts a new window that resets at 8 h.
            let reset = if *ts >= 3 * HOUR { 8 * HOUR } else { 5 * HOUR };
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'session', {pct}, {}, 'reading', NULL);",
                june + ts,
                june + reset
            ));
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'weekly', {}, NULL, 'reading', NULL);",
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
            "INSERT INTO limit_samples VALUES (2, 'claude-code', 'c', {}, 'session', 100, {}, 'rejected', NULL);",
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
        let r = report(&store.conn, &BTreeMap::new(), None, now, &Utc).unwrap();
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
        // The fall from 60 % to 0 % came before the window's reset time.
        let u = session.utilization.as_ref().unwrap();
        assert_eq!((u.windows, u.exhausted, u.mid_window_resets), (2, 0, 1));
        // The first window was reset early, so only the second counts.
        assert_eq!(u.cut_short, 1);
        assert_eq!(u.mean_peak_percent, Some(25.0));
        let sp = session.spread.as_ref().unwrap();
        assert_eq!(
            (sp.tokens_min, sp.tokens_median, sp.tokens_max),
            (1200.0, 1600.0, 2000.0)
        );
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
        // June is partial (the first month), so it is projected over 30 days.
        let june = &codex.months[0];
        assert!(
            june.days_covered > 21.0 && june.days_covered < 22.0,
            "{}",
            june.days_covered
        );
        let projected = june.projected_cost_usd.unwrap();
        assert!((projected - 1.3 / june.days_covered * 30.0).abs() < 1e-9);
        assert!(codex.credits.is_none());
    }

    #[test]
    fn claude_rejections_measure_a_full_window_and_weekly_is_a_lower_bound() {
        let store = fixture();
        let r = report(&store.conn, &BTreeMap::new(), None, 1_792_500_000_000, &Utc).unwrap();
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
        let r = report(&store.conn, &BTreeMap::new(), None, 1_792_500_000_000, &Utc).unwrap();
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
        let r = report(&store.conn, &subs, None, 1_792_500_000_000, &Utc).unwrap();
        assert_eq!(r.tools[1].subscription.name, "ChatGPT Pro");
        let multiple = r.tools[1].months[0].multiple.unwrap();
        assert!((multiple - 1.3 / 200.0).abs() < 1e-9, "{multiple}");
    }

    #[test]
    fn a_run_ends_at_the_first_reading_of_its_peak() {
        let r = Some(5 * HOUR);
        let samples = vec![
            s(0, 0.0, r),
            s(HOUR, 60.0, r),
            s(2 * HOUR, 100.0, r),
            // at the limit: later requests are paid with credits or refused
            s(3 * HOUR, 100.0, r),
            s(4 * HOUR, 100.0, r),
        ];
        assert_eq!(
            runs_of(&samples, SESSION_WINDOW_MS),
            vec![(0, 2 * HOUR, 0.0, 100.0, 5)]
        );
        // Reset times that differ by provider jitter are one window.
        let jitter = vec![
            s(0, 10.0, Some(5 * HOUR)),
            s(HOUR, 40.0, Some(5 * HOUR + 950)),
        ];
        assert_eq!(runs_of(&jitter, SESSION_WINDOW_MS).len(), 1);
    }

    #[test]
    fn a_refusal_closes_a_live_run_of_the_same_window() {
        let r = Some(5 * HOUR);
        let samples = vec![
            s(0, 0.0, r),
            s(HOUR, 50.0, r),
            Sample {
                ts_ms: 2 * HOUR,
                used_percent: 100.0,
                resets_at_ms: r,
                rejected: true,
            },
        ];
        assert_eq!(
            runs_of(&samples, SESSION_WINDOW_MS),
            vec![(0, 2 * HOUR, 0.0, 100.0, 3)]
        );
    }

    #[test]
    fn pools_take_the_models_named_last() {
        let models: Vec<String> = [
            "gemini-3.8-flash",
            "gemini-4-argon",
            "claude-opus-4-6",
            "gpt-oss-120b",
            "claude-fable-5-1",
        ]
        .map(String::from)
        .to_vec();
        let pools: Vec<String> = ["Argon", "Claude & GPT", "Gemini"]
            .map(String::from)
            .to_vec();
        assert_eq!(pool_models("Gemini", &pools, &models), BTreeSet::from([0]));
        assert_eq!(pool_models("Argon", &pools, &models), BTreeSet::from([1]));
        assert_eq!(
            pool_models("Claude & GPT", &pools, &models),
            BTreeSet::from([2, 3, 4])
        );
        let fable = vec!["Fable".to_owned()];
        assert_eq!(pool_models("Fable", &fable, &models), BTreeSet::from([4]));
    }

    #[test]
    fn credits_spent_at_the_limit_are_left_out_of_the_plan_value() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = 1_781_000_000_000i64;
        let mut sql = String::from(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'codex', '/l1');
             INSERT INTO sessions (tool, session_id) VALUES ('codex', 'a');",
        );
        // 0 -> 100 % over two hours, then two hours at 100 % paid with credits,
        // then a mid-window reset that costs 50 credits.
        for (ts, pct, bal) in [
            (0, 0.0, 500.0),
            (HOUR, 50.0, 500.0),
            (2 * HOUR, 100.0, 500.0),
            (2 * HOUR + 30 * 60_000, 100.0, 490.0),
            // a concurrent session's older balance
            (2 * HOUR + 31 * 60_000, 100.0, 500.0),
            (3 * HOUR, 100.0, 480.0),
            (4 * HOUR, 100.0, 470.0),
            (4 * HOUR + 60_000, 0.0, 420.0),
        ] {
            let reset = if pct == 0.0 && ts > 0 {
                ts + 5 * HOUR
            } else {
                5 * HOUR
            };
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'session', {pct}, {}, 'reading', {bal});",
                t0 + ts,
                t0 + reset
            ));
        }
        for (k, ts, cost) in [
            (0, HOUR - 1, 1.0),
            (1, 2 * HOUR - 1, 1.0),
            (2, 3 * HOUR - 1, 4.0),
        ] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('codex', 'r{k}', 'a', 1, {}, 'm', 100, 0, {cost});",
                t0 + ts
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let r = report(
            &store.conn,
            &BTreeMap::new(),
            None,
            t0 + 40 * 24 * HOUR,
            &Utc,
        )
        .unwrap();
        let codex = &r.tools[1];
        let run = &codex.windows[0].runs[0];
        assert_eq!(
            run.usage.requests, 2,
            "the run stops at the first 100 % reading"
        );
        let c = codex.credits.as_ref().unwrap();
        assert_eq!(
            (c.spent, c.spent_at_limit, c.spent_near_resets),
            (80.0, 30.0, 50.0)
        );
        assert_eq!(c.credit_funded.requests, 1);
        assert_eq!(c.credit_funded.estimated_cost_usd, Some(4.0));
        let month = &codex.months[0];
        assert_eq!(month.usage.estimated_cost_usd, Some(6.0));
        assert_eq!(month.credit_funded_cost_usd, Some(4.0));
        assert!((month.multiple.unwrap() - 2.0 / 20.0).abs() < 1e-9);
        let u = codex.windows[0].utilization.as_ref().unwrap();
        assert_eq!((u.exhausted, u.mid_window_resets), (1, 1));
        let f = findings(&r).join("\n");
        assert!(f.contains("80 spent, 420 left."), "{f}");
        assert!(
            f.contains("codex credits, 2026-06-09 to 2026-06-09: "),
            "{f}"
        );
        assert!(f.contains("codex limits: 1 of 2 5-hour windows hit 100 %, 1 reset early; the rest peaked at 0 % on average."), "{f}");
        assert!(f.contains("30 paid for 1 requests at a full window"), "{f}");
    }

    #[test]
    fn live_pool_readings_measure_only_the_pool_models() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = 1_792_000_000_000i64;
        let reset = t0 + 7 * 24 * HOUR - HOUR;
        let mut sql = String::from(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'claude-code', '/l1');
             INSERT INTO sessions (tool, session_id) VALUES ('claude-code', 'a');",
        );
        for (ts, pct) in [(0, 10.0), (HOUR, 30.0), (2 * HOUR, 60.0)] {
            sql.push_str(&format!(
                "INSERT INTO live_limits VALUES ('claude-code', 'Fable', 'weekly', {}, {pct}, {reset});",
                t0 + ts
            ));
        }
        for (k, ts, model, tok) in [
            (0, 30 * 60_000, "claude-fable-5-1", 1000),
            (1, 40 * 60_000, "claude-haiku-5-5", 9000),
        ] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, canonical_model, input_tokens, output_tokens, cost_usd)
                 VALUES ('claude-code', 'r{k}', 'a', 1, {}, '{model}', '{model}', {tok}, 0, 1.0);",
                t0 + ts
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let r = report(&store.conn, &BTreeMap::new(), None, t0 + 3 * HOUR, &Utc).unwrap();
        let claude = &r.tools[0];
        assert_eq!(claude.limit_source, "live");
        let pool = claude.windows.iter().find(|w| w.pool.is_some()).unwrap();
        assert_eq!(pool.pool.as_deref(), Some("Fable"));
        assert_eq!(pool.models, vec!["claude-fable-5-1".to_string()]);
        assert_eq!(pool.live_readings, 3);
        // 10 -> 60 %: one Fable request of 1,000 tokens, doubled.
        assert_eq!(pool.per_window.as_ref().unwrap().tokens, 2000.0);
    }

    #[test]
    fn drift_wording_separates_model_prices_from_the_window() {
        let cap = |cost: f64, reference: f64, tokens: f64| Capacity {
            tokens,
            estimated_cost_usd: Some(cost),
            reference_cost_usd: Some(reference),
            ..Capacity::default()
        };
        let point = |month: &str, c: Capacity, model: &str| DriftPoint {
            month: month.into(),
            runs: 5,
            per_window: c,
            main_model: Some(model.into()),
            main_model_share: Some(0.8),
        };
        let mut w = WindowEstimate {
            window: "session".into(),
            window_hours: 5,
            readings: 0,
            qualifying_runs: 10,
            runs: vec![],
            per_window: None,
            by_model: vec![],
            drift: vec![
                point("2026-05", cap(26.0, 20.0, 27e6), "gpt-5.5"),
                DriftPoint {
                    runs: 1,
                    ..point("2026-07", cap(90.0, 90.0, 1e6), "x")
                },
                point("2026-10", cap(10.0, 19.0, 50e6), "gpt-6.1-sol"),
            ],
            lower_bound: None,
            pool: None,
            models: vec![],
            live_readings: 0,
            first_reading: None,
            last_reading: None,
            spread: None,
            utilization: None,
            latest: None,
        };
        let mut t = ToolValue {
            tool: "codex".into(),
            subscription: default_subscription("codex"),
            limit_source: "readings".into(),
            windows: vec![],
            months: vec![],
            complete_months: 0,
            mean_monthly_cost_usd: None,
            mean_multiple: None,
            reference_model: Some("gpt-6.1-sol".into()),
            first_request: None,
            last_request: None,
            pace: None,
            credits: None,
            session_shares: vec![],
        };
        let s = drift_sentence(&t, &w).unwrap();
        assert!(
            s.starts_with("5-hour window, 2026-05 to 2026-10: est. $26 to $10 (-62 %)"),
            "{s}"
        );
        assert!(s.contains("tokens 27.0M to 50.0M (+85 %)"), "{s}");
        assert!(s.contains("At gpt-6.1-sol rates throughout: $20 to $19 (-5 %), so it holds about the same work and cheaper models moved its value."), "{s}");
        assert!(s.ends_with("Main model: gpt-5.5, then gpt-6.1-sol."), "{s}");
        w.drift[2].per_window.reference_cost_usd = Some(10.0);
        t.reference_model = Some("gpt-6.1-sol".into());
        assert!(
            drift_sentence(&t, &w)
                .unwrap()
                .contains("so it holds less work.")
        );
        w.drift.truncate(2);
        assert!(
            drift_sentence(&t, &w).is_none(),
            "one solid month is not a trend"
        );
    }

    #[test]
    fn session_share_pools_the_weekly_rise_of_each_session_window() {
        let r = |session, sr, weekly, wr| PairedReading {
            ts: sr,
            session,
            session_reset: Some(sr),
            weekly,
            weekly_reset: Some(wr),
        };
        let week = 900_000_000;
        let rows = [
            // One window: 0 to 50 % of session, 10 to 18 % of week.
            r(0.0, 1_000, 10.0, week),
            r(20.0, 1_000, 13.0, week),
            // A stale reading from a concurrent session changes nothing.
            r(10.0, 1_000, 11.0, week),
            r(50.0, 1_000, 18.0, week),
            // The next window: 0 to 100 %, 18 to 34 %.
            r(0.0, 20_000_000, 18.0, week),
            r(100.0, 20_000_000, 34.0, week),
            // Too small a rise to count.
            r(0.0, 40_000_000, 34.0, week),
            r(5.0, 40_000_000, 35.0, week),
        ];
        let (windows, share) = share_of(&rows, |_, _| Some(1.0));
        assert_eq!(windows, 2);
        assert!((share.unwrap() - 100.0 * 24.0 / 150.0).abs() < 1e-9);
        // A pool that took half of every window's cost fills twice as fast
        // when a session is spent on it alone.
        let (_, half) = share_of(&rows, |_, _| Some(0.5));
        assert!((half.unwrap() - 2.0 * share.unwrap()).abs() < 1e-9);
    }

    #[test]
    fn dollars_and_tokens_read_short() {
        assert_eq!(dollars(1296.71), "$1,297");
        assert_eq!(dollars(9.4), "$9");
        assert_eq!(short_tokens(3_199_200_000.0), "3.2B");
        assert_eq!(short_tokens(29_234_515.0), "29.2M");
        assert_eq!(short_tokens(45_000.0), "45k");
    }
}
