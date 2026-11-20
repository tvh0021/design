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
use chrono::{Datelike, NaiveDateTime, Offset, TimeZone};
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

/// One `[subscriptions]` line: a plan, or one seat of a plan. A tool can
/// have several (two ChatGPT Plus seats, or Google AI Pro and Ultra).
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub name: String,
    /// List price a month; `None` for `none`.
    pub monthly_usd: Option<f64>,
    /// What is paid a month (`paid=`); the list price unless set.
    pub paid_monthly_usd: Option<f64>,
    /// The `ai-usage` fingerprint of the account the seat signs in as.
    pub account: Option<String>,
    /// A private name for the seat ("Home"); never on the public page.
    pub label: Option<String>,
    /// Local start; `None` for always.
    pub from: Option<NaiveDateTime>,
    /// Local end, exclusive; `None` while the plan runs.
    pub until: Option<NaiveDateTime>,
    /// Used on another machine: none of its readings or requests are here.
    pub remote: bool,
}

/// Built-in plans, overridable through `[subscriptions]` in config.toml.
pub fn default_subscription(tool: &str) -> Plan {
    let (name, price) = match tool {
        "claude-code" => ("Claude Team, premium seat", Some(125.0)),
        "codex" => ("ChatGPT Plus", Some(20.0)),
        "antigravity" => ("Google AI Pro", Some(19.99)),
        _ => ("unknown plan", None),
    };
    Plan {
        name: name.to_owned(),
        monthly_usd: price,
        paid_monthly_usd: price,
        account: None,
        label: None,
        from: None,
        until: None,
        remote: false,
    }
}

/// "ChatGPT Plus × 2" for two seats of one plan, the name alone for one.
pub fn tier_label(name: &str, seats: i64) -> String {
    if seats > 1 {
        format!("{name} \u{d7} {seats}")
    } else {
        name.to_owned()
    }
}

/// One plan line as the report shows it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PlanValue {
    pub name: String,
    pub monthly_usd: Option<f64>,
    pub paid_monthly_usd: Option<f64>,
    /// 1, 2, ... among the lines of one plan and price; null for a lone line.
    pub seat: Option<i64>,
    pub account: Option<String>,
    pub label: Option<String>,
    /// Local "YYYY-MM-DDTHH:MM"; `until` is exclusive.
    pub from: Option<String>,
    pub until: Option<String>,
    pub remote: bool,
    /// The plan runs now.
    pub active: bool,
}

/// What a tool's plans are and cost now.
#[derive(Debug, Clone, PartialEq, Serialize, Default)]
pub struct Subscription {
    /// The current plans: "ChatGPT Plus × 2", "Google AI Pro + Google AI Ultra".
    pub name: String,
    /// List price a month of the current plans.
    pub monthly_usd: Option<f64>,
    /// What the current plans cost a month.
    pub paid_monthly_usd: Option<f64>,
    pub plans: Vec<PlanValue>,
}

/// A plan line with its span in epoch ms and its place among the tiers.
struct Seat {
    plan: Plan,
    from_ms: i64,
    until_ms: i64,
    tier: usize,
    seat: Option<i64>,
}

impl Seat {
    fn active_at(&self, ts: i64) -> bool {
        self.from_ms <= ts && ts < self.until_ms
    }

    fn overlap(&self, a: i64, b: i64) -> i64 {
        (self.until_ms.min(b) - self.from_ms.max(a)).max(0)
    }
}

/// The epoch ms of a local time. A time skipped by a clock change reads as
/// the moment the clock jumped: the first local minute after it that exists.
fn local_ms(t: NaiveDateTime, tz: &impl TimeZone) -> i64 {
    (0..=48 * 60)
        .find_map(|m| {
            tz.from_local_datetime(&(t + chrono::Duration::minutes(m)))
                .earliest()
        })
        .map_or_else(
            // No zone skips two days; read it at the offset in force then.
            || {
                let off = tz.offset_from_utc_datetime(&t).fix();
                (t - off).and_utc().timestamp_millis()
            },
            |d| d.timestamp_millis(),
        )
}

/// A tool's plans, grouped into tiers of one name and list price, and how a
/// reading or request is assigned to an account.
struct Plans {
    seats: Vec<Seat>,
    /// (name, list price) of each tier, in config order.
    tiers: Vec<(String, Option<f64>)>,
    /// Accounts with readings or sessions here, or named by a local plan.
    known: BTreeSet<String>,
}

impl Plans {
    fn load(
        conn: &Connection,
        tool: &str,
        subscriptions: &BTreeMap<String, Vec<Plan>>,
        tz: &impl TimeZone,
    ) -> Result<Self> {
        let mut known: BTreeSet<String> = BTreeSet::new();
        for sql in [
            "SELECT DISTINCT account FROM live_limits WHERE tool = ?1 AND account != '' AND lower(pool) != 'claude & gpt'",
            "SELECT DISTINCT account FROM limit_samples WHERE tool = ?1 AND account IS NOT NULL",
            "SELECT DISTINCT account FROM sessions WHERE tool = ?1 AND account IS NOT NULL",
        ] {
            known.extend(
                conn.prepare_cached(sql)?
                    .query_map(params![tool], |r| r.get::<_, String>(0))?
                    .collect::<rusqlite::Result<Vec<_>>>()?,
            );
        }
        let list = subscriptions
            .get(tool)
            .filter(|l| !l.is_empty())
            .cloned()
            .unwrap_or_else(|| vec![default_subscription(tool)]);
        Ok(Self::new(list, known, tz))
    }

    fn new(list: Vec<Plan>, mut known: BTreeSet<String>, tz: &impl TimeZone) -> Self {
        let mut tiers: Vec<(String, Option<f64>)> = Vec::new();
        let mut seats: Vec<Seat> = list
            .into_iter()
            .map(|plan| {
                let key = (plan.name.clone(), plan.monthly_usd);
                let tier = tiers.iter().position(|t| *t == key).unwrap_or_else(|| {
                    tiers.push(key);
                    tiers.len() - 1
                });
                Seat {
                    from_ms: plan.from.map_or(i64::MIN, |t| local_ms(t, tz)),
                    until_ms: plan.until.map_or(i64::MAX, |t| local_ms(t, tz)),
                    tier,
                    seat: None,
                    plan,
                }
            })
            .collect();
        for t in 0..tiers.len() {
            let n = seats.iter().filter(|s| s.tier == t).count();
            if n > 1 {
                for (i, s) in seats.iter_mut().filter(|s| s.tier == t).enumerate() {
                    s.seat = Some(i as i64 + 1);
                }
            }
        }
        // A remote seat's account is never read here, so it does not make
        // the local readings ambiguous.
        known.extend(
            seats
                .iter()
                .filter(|s| !s.plan.remote)
                .filter_map(|s| s.plan.account.clone()),
        );
        Plans {
            seats,
            tiers,
            known,
        }
    }

    /// The account a reading or request belongs to, and the tier it counts
    /// toward. A row with no account goes to the only local plan with an
    /// account that runs at `ts`; failing that, to the tool's only account
    /// (or to one unnamed stream when there is none); with several accounts
    /// and no way to choose, to nobody (`None`).
    fn resolve(&self, raw: Option<&str>, ts: i64) -> Option<(usize, String)> {
        self.resolve_seat(raw, ts)
            .map(|(tier, _, account)| (tier, account))
    }

    /// As `resolve`, also returning whether the resolved seat itself runs at `ts`.
    fn resolve_seat(&self, raw: Option<&str>, ts: i64) -> Option<(usize, bool, String)> {
        let account = match raw.map(str::trim).filter(|a| !a.is_empty()) {
            Some(a) => a.to_owned(),
            None => {
                let active: BTreeSet<&str> = self
                    .seats
                    .iter()
                    .filter(|s| !s.plan.remote && s.active_at(ts))
                    .filter_map(|s| s.plan.account.as_deref())
                    .collect();
                if active.len() == 1 {
                    active.first().map(|a| (*a).to_owned())?
                } else if self.known.len() <= 1 {
                    self.known.first().cloned().unwrap_or_default()
                } else {
                    return None;
                }
            }
        };
        let seat = self.seat_at(&account, ts);
        let tier = seat.map_or(0, |s| s.tier);
        let counts = seat.is_some_and(|s| s.active_at(ts));
        Some((tier, counts, account))
    }

    /// The plan that covers `account` at `ts`: one naming it (active at `ts`
    /// when several do), else an unnamed local plan, else any local one.
    fn seat_at(&self, account: &str, ts: i64) -> Option<&Seat> {
        let pick = |f: &dyn Fn(&Seat) -> bool| {
            let mut matching = self.seats.iter().filter(|s| f(s)).peekable();
            let first = matching.peek().copied();
            matching.find(|s| s.active_at(ts)).or(first)
        };
        pick(&|s| s.plan.account.as_deref() == Some(account))
            .or_else(|| pick(&|s| s.plan.account.is_none() && !s.plan.remote))
            .or_else(|| pick(&|s| !s.plan.remote))
    }

    /// A reading of `account` at `ts` falls outside the plan that covers it,
    /// so it measures no plan: a run must not straddle a plan's start or end.
    fn off_plan(&self, account: &str, ts: i64) -> bool {
        self.seat_at(account, ts).is_some_and(|s| !s.active_at(ts))
    }

    fn seat_of(&self, tier: usize, account: &str) -> Option<&Seat> {
        self.seats
            .iter()
            .find(|s| s.tier == tier && s.plan.account.as_deref() == Some(account))
    }

    /// The tier of the first local plan running at `now`.
    fn primary_tier(&self, now_ms: i64) -> usize {
        let local = || self.seats.iter().filter(|s| !s.plan.remote);
        local()
            .find(|s| s.active_at(now_ms))
            .or_else(|| local().next())
            .map_or(0, |s| s.tier)
    }

    /// (list, paid) price a month over `[a, b)`: each plan's price weighted
    /// by the share of the span it ran. Null when no plan has that price.
    fn price_over(&self, a: i64, b: i64) -> (Option<f64>, Option<f64>) {
        if b <= a {
            return (None, None);
        }
        let span = (b - a) as f64;
        let sum = |f: fn(&Plan) -> Option<f64>| {
            let mut out = None::<f64>;
            for s in &self.seats {
                if let Some(p) = f(&s.plan) {
                    *out.get_or_insert(0.0) += p * s.overlap(a, b) as f64 / span;
                }
            }
            out
        };
        (sum(|p| p.monthly_usd), sum(|p| p.paid_monthly_usd))
    }

    fn subscription(&self, now_ms: i64) -> Subscription {
        let active: Vec<&Seat> = self.seats.iter().filter(|s| s.active_at(now_ms)).collect();
        // With every plan ended, the name still says what the tool was on.
        let named = if active.is_empty() {
            self.seats.iter().collect()
        } else {
            active.clone()
        };
        let name = (0..self.tiers.len())
            .filter_map(|t| {
                let n = named.iter().filter(|s| s.tier == t).count() as i64;
                (n > 0).then(|| tier_label(&self.tiers[t].0, n))
            })
            .collect::<Vec<_>>()
            .join(" + ");
        let sum = |f: fn(&Plan) -> Option<f64>| {
            active
                .iter()
                .filter_map(|s| f(&s.plan))
                .fold(None, |a: Option<f64>, p| Some(a.unwrap_or(0.0) + p))
        };
        let when = |t: Option<NaiveDateTime>| t.map(|t| t.format("%Y-%m-%dT%H:%M").to_string());
        Subscription {
            name,
            monthly_usd: sum(|p| p.monthly_usd),
            paid_monthly_usd: sum(|p| p.paid_monthly_usd),
            plans: self
                .seats
                .iter()
                .map(|s| PlanValue {
                    name: s.plan.name.clone(),
                    monthly_usd: s.plan.monthly_usd,
                    paid_monthly_usd: s.plan.paid_monthly_usd,
                    seat: s.seat,
                    account: s.plan.account.clone(),
                    label: s.plan.label.clone(),
                    from: when(s.plan.from),
                    until: when(s.plan.until),
                    remote: s.plan.remote,
                    active: s.active_at(now_ms),
                })
                .collect(),
        }
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
    /// Closed windows by how full they got: under 25 %, 25 to 75 %, 75 % to
    /// just under the limit, and out (100 % or a refusal).
    pub peak_bands: [i64; 4],
    pub by_month: Vec<UtilizationMonth>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Default)]
pub struct WindowEstimate {
    /// The plan these windows belong to: its name, shared by every seat.
    pub plan: String,
    /// Accounts whose readings are pooled here (two ChatGPT Plus seats: 2).
    pub seats: i64,
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
    /// The newest reading of each seat, by seat.
    pub latest_by_seat: Vec<SeatLatest>,
    /// Readings left out because no account could be told for them: rows
    /// written before readings named their account, at a time when two
    /// accounts were in use.
    pub unattributed_readings: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SeatLatest {
    pub seat: Option<i64>,
    pub account: Option<String>,
    pub label: Option<String>,
    pub at: String,
    pub used_percent: f64,
    pub resets_at: Option<String>,
    /// The reading was sampled live by `limits record`.
    pub live: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Latest {
    pub at: String,
    pub used_percent: f64,
    pub resets_at: Option<String>,
    /// The reading was sampled live by `limits record`.
    pub live: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MonthValue {
    /// "2026-06"
    pub month: String,
    pub complete: bool,
    #[serde(flatten)]
    pub usage: Tokens,
    /// Plan-covered estimated cost over `price_usd`; null without either.
    /// Requests paid with credits past the limit are left out.
    pub multiple: Option<f64>,
    /// List price of the plans over the month, each weighted by the share of
    /// the month it ran.
    pub price_usd: Option<f64>,
    /// What was paid over the month, weighted the same way.
    pub paid_usd: Option<f64>,
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
    /// `monthly_cost_usd` over `price_usd`.
    pub multiple: Option<f64>,
    /// List price a month of the plans over the same days, weighted by the
    /// share of them each plan ran.
    pub price_usd: Option<f64>,
    /// What the plans cost a month over the same days.
    pub paid_usd: Option<f64>,
    /// Plan lines with a list price that ran any of those days, so a plan
    /// retired within them is counted with the price it adds.
    pub plans: i64,
}

/// How much of a weekly window one full session window uses, read from
/// paired readings of the two: within each session window, the weekly
/// share's rise over the session share's rise.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SessionShare {
    /// The plan the windows belong to.
    pub plan: String,
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

#[derive(Debug, Clone, PartialEq, Serialize, Default)]
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
    /// Accounts with readings in the last `PACE_DAYS` days at times no plan
    /// line names them. Private: the dashboard shows them only locally.
    pub unplanned_accounts: Vec<String>,
}

/// All plans together, at their recent pace.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Combined {
    /// Plans running now with a list price.
    pub plans: i64,
    /// List price a month of the plans running now.
    pub monthly_usd: f64,
    /// What the plans running now cost a month.
    pub paid_monthly_usd: f64,
    /// The paced tools' `pace.plans`, added up: the plans behind the pace
    /// figures, which may include one retired within the pace days.
    pub pace_plans: i64,
    /// The tools' `pace.price_usd` and `pace.paid_usd`, added up.
    pub pace_price_usd: Option<f64>,
    pub pace_paid_usd: Option<f64>,
    pub pace_monthly_cost_usd: Option<f64>,
    /// `pace_monthly_cost_usd` over `pace_price_usd`.
    pub pace_multiple: Option<f64>,
    /// `pace_monthly_cost_usd` over `pace_paid_usd`.
    pub pace_paid_multiple: Option<f64>,
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
    live: bool,
}

/// One request, reduced to what the report needs.
struct Req {
    ts: i64,
    session: u32,
    model: u32,
    /// The account it ran under (see `Plans::resolve`); `None` when no
    /// account can be told, which keeps it out of every window.
    account: Option<u32>,
    /// The tier it counts toward: its account's, and only while a plan of
    /// that tier runs; `None` keeps it out of every tier's windows.
    tier: Option<usize>,
    /// A local plan of its account runs at `ts` (or, with no account told,
    /// any local plan does): only these count toward a value multiple.
    covered: bool,
    input: i64,
    cache_read: i64,
    cache_write: i64,
    output: i64,
    cost: Option<f64>,
    reference_cost: Option<f64>,
}

/// A tool's requests in time order, with interned session, model and
/// account names.
struct Data {
    reqs: Vec<Req>,
    models: Vec<String>,
    account_ids: HashMap<String, u32>,
}

impl Data {
    fn load(conn: &Connection, tool: &str, plans: &Plans) -> Result<Self> {
        // Every account a reading can resolve to gets an id up front.
        let mut account_ids: HashMap<String, u32> = HashMap::new();
        for a in plans
            .known
            .iter()
            .cloned()
            .chain(plans.seats.iter().filter_map(|s| s.plan.account.clone()))
            .chain([String::new()])
        {
            let n = account_ids.len() as u32;
            account_ids.entry(a).or_insert(n);
        }
        let mut stmt = conn.prepare_cached(
            "SELECT r.ts_ms, r.session_id, COALESCE(r.canonical_model, r.model, ''), r.input_tokens,
                    r.cache_read_input_tokens, r.cache_creation_input_tokens, r.output_tokens,
                    r.cost_usd, s.account
             FROM requests r LEFT JOIN sessions s ON s.tool = r.tool AND s.session_id = r.session_id
             WHERE r.tool = ?1 ORDER BY r.ts_ms",
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
            let ts: i64 = r.get(0)?;
            let raw: Option<String> = r.get(8)?;
            let resolved = plans.resolve_seat(raw.as_deref(), ts);
            let tier = resolved
                .as_ref()
                .filter(|(_, counts, _)| *counts)
                .map(|(t, ..)| *t);
            let covered = match &resolved {
                Some((_, counts, _)) => *counts,
                None => plans
                    .seats
                    .iter()
                    .any(|s| !s.plan.remote && s.active_at(ts)),
            };
            let account = resolved.map(|(_, _, a)| {
                let n = account_ids.len() as u32;
                *account_ids.entry(a).or_insert(n)
            });
            reqs.push(Req {
                ts,
                session,
                model,
                account,
                tier,
                covered,
                input: r.get(3)?,
                cache_read: r.get(4)?,
                cache_write: r.get(5)?,
                output: r.get(6)?,
                cost: r.get(7)?,
                reference_cost: None,
            });
        }
        Ok(Data {
            reqs,
            models,
            account_ids,
        })
    }

    /// The id of a resolved account; every one has an id from `load`.
    fn account_id(&self, account: &str) -> u32 {
        self.account_ids.get(account).copied().unwrap_or(u32::MAX)
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

/// Which requests a window's limit covers: every model, or a pool's models,
/// of one account, and of one tier while it runs. `None` matches any.
#[derive(Clone, Copy)]
struct Scope<'a> {
    models: Option<&'a BTreeSet<u32>>,
    account: Option<u32>,
    tier: Option<usize>,
}

impl Scope<'_> {
    fn covers(&self, r: &Req) -> bool {
        self.models.is_none_or(|m| m.contains(&r.model)) && self.of_stream(r)
    }

    /// The account and tier match, whatever the model.
    fn of_stream(&self, r: &Req) -> bool {
        self.account.is_none_or(|a| r.account == Some(a))
            && self.tier.is_none_or(|t| r.tier == Some(t))
    }
}

fn pool_words(pool: &str) -> Vec<String> {
    pool.to_ascii_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|w| !w.is_empty() && *w != "and")
        .map(str::to_owned)
        .collect()
}

/// Live pools left out of the report.
const EXCLUDED_POOLS: &[&str] = &["Claude & GPT"];

fn pool_excluded(pool: &str) -> bool {
    EXCLUDED_POOLS.iter().any(|p| p.eq_ignore_ascii_case(pool))
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

/// One account's readings of one window: (tier, account, window, pool).
/// Accounts never share a series, since each has its own windows.
type StreamKey = (usize, String, &'static str, String);

/// A tool's logged and live readings, split by account and window.
struct Readings {
    /// Each stream's readings in time order.
    streams: BTreeMap<StreamKey, Vec<Sample>>,
    /// (window, pool) -> readings no account could be told for.
    unattributed: BTreeMap<(&'static str, String), i64>,
    /// (account, time) of readings left out for falling outside every plan
    /// of their account; kept only to name unplanned accounts.
    off_plan: Vec<(String, i64)>,
}

impl Readings {
    fn load(conn: &Connection, tool: &str, plans: &Plans) -> Result<Self> {
        let mut stmt = conn.prepare_cached(
            "SELECT ts_ms, used_percent, resets_at_ms, kind = 'rejected', 0, window, '', account
                 FROM limit_samples WHERE tool = ?1
             UNION ALL
             SELECT ts_ms, used_percent, resets_at_ms, 0, 1, window, pool, account
                 FROM live_limits WHERE tool = ?1
             ORDER BY 1, 4",
        )?;
        let mut out = Readings {
            streams: BTreeMap::new(),
            unattributed: BTreeMap::new(),
            off_plan: Vec::new(),
        };
        let mut rows = stmt.query(params![tool])?;
        while let Some(r) = rows.next()? {
            let ts_ms: i64 = r.get(0)?;
            let window = if r.get::<_, String>(5)? == "weekly" {
                "weekly"
            } else {
                "session"
            };
            let pool: String = r.get(6)?;
            if pool_excluded(&pool) {
                continue;
            }
            let raw: Option<String> = r.get(7)?;
            let Some((tier, account)) = plans.resolve(raw.as_deref(), ts_ms) else {
                *out.unattributed.entry((window, pool)).or_default() += 1;
                continue;
            };
            if plans.off_plan(&account, ts_ms) {
                out.off_plan.push((account, ts_ms));
                continue;
            }
            out.streams
                .entry((tier, account, window, pool))
                .or_default()
                .push(Sample {
                    ts_ms,
                    used_percent: r.get(1)?,
                    resets_at_ms: r.get(2)?,
                    rejected: r.get(3)?,
                    live: r.get(4)?,
                });
        }
        Ok(out)
    }

    /// The streams of one tier's window: (account, readings).
    fn of(&self, tier: usize, window: &str, pool: &str) -> Vec<(&str, &[Sample])> {
        self.streams
            .iter()
            .filter(|((t, _, w, p), _)| *t == tier && *w == window && p == pool)
            .map(|((_, a, ..), s)| (a.as_str(), s.as_slice()))
            .collect()
    }

    /// Accounts with any reading in `tier`.
    fn accounts(&self, tier: usize) -> BTreeSet<&str> {
        self.streams
            .keys()
            .filter(|k| k.0 == tier)
            .map(|k| k.1.as_str())
            .collect()
    }
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

/// account id -> the spans in which its requests were paid with credits.
type Funded = HashMap<u32, Spans>;

fn is_funded(funded: &Funded, r: &Req) -> bool {
    r.account
        .and_then(|a| funded.get(&a))
        .is_some_and(|s| in_spans(r.ts, s))
}

/// One account's closed window instances, from its readings (in time order)
/// and its mid-window resets.
fn window_peaks(
    samples: &[Sample],
    window_len_ms: i64,
    resets: &[i64],
    now_ms: i64,
) -> Vec<WindowPeak> {
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
    by_reset
}

/// How full window instances got, from one account or several; `resets` is
/// how many mid-window resets they had.
fn utilization(
    by_reset: &[WindowPeak],
    window_len_ms: i64,
    resets: usize,
    tz: &impl TimeZone,
) -> Option<Utilization> {
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
    let mut peak_bands = [0; 4];
    for w in by_reset {
        let band = match w.1 {
            _ if exhausted(w) => 3,
            Some(p) if p < 25.0 => 0,
            Some(p) if p < 75.0 => 1,
            Some(_) => 2,
            None => continue,
        };
        peak_bands[band] += 1;
    }
    let mut months: BTreeMap<String, Vec<&WindowPeak>> = BTreeMap::new();
    for w in by_reset {
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
        mid_window_resets: resets as i64,
        peak_bands,
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

/// How full one tool's plan-wide limit windows of one length got.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LimitBands {
    pub tool: String,
    /// `session` or `weekly`.
    pub window: String,
    pub window_hours: i64,
    /// Closed windows counted.
    pub windows: i64,
    /// As [`Utilization::peak_bands`].
    pub bands: [i64; 4],
}

/// Peak bands of each tool's plan-wide windows, from readings taken since
/// `since_ms` (all of them when `None`), every account's windows counted on
/// their own. Pools are left out: their windows sit inside the plan's.
pub fn limit_bands(
    conn: &Connection,
    tools: &[&str],
    subscriptions: &BTreeMap<String, Vec<Plan>>,
    since_ms: Option<i64>,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Result<Vec<LimitBands>> {
    let mut readings = Vec::new();
    for tool in tools {
        let plans = Plans::load(conn, tool, subscriptions, tz)?;
        readings.push(Readings::load(conn, tool, &plans)?);
    }
    let mut out = Vec::new();
    for window in ["session", "weekly"] {
        let len = window_ms(window);
        for (tool, readings) in tools.iter().zip(&readings) {
            let (mut peaks, mut resets) = (Vec::new(), 0);
            for ((_, _, w, pool), samples) in &readings.streams {
                if *w != window || !pool.is_empty() {
                    continue;
                }
                let from = since_ms.map_or(0, |t| samples.partition_point(|s| s.ts_ms < t));
                let samples = &samples[from..];
                let r = mid_window_resets(samples);
                resets += r.len();
                peaks.extend(window_peaks(samples, len, &r, now_ms));
            }
            if let Some(u) = utilization(&peaks, len, resets, tz) {
                out.push(LimitBands {
                    tool: (*tool).to_owned(),
                    window: window.to_owned(),
                    window_hours: len / 3_600_000,
                    windows: u.peak_bands.iter().sum(),
                    bands: u.peak_bands,
                });
            }
        }
    }
    Ok(out)
}

/// A logged credit reading: time, session %, weekly %, balance.
type CreditPoint = (i64, Option<f64>, Option<f64>, f64);

/// Credit balance changes from the logged Codex readings, each account's
/// balance on its own, and the spans in which each account's requests were
/// paid with credits past the limit. `resets` holds each account's
/// mid-window resets of the plan's windows.
fn credits(
    conn: &Connection,
    tool: &str,
    plans: &Plans,
    resets: &HashMap<String, Vec<i64>>,
    data: &Data,
) -> Result<Option<(CreditUse, Funded)>> {
    let mut stmt = conn.prepare_cached(
        "SELECT ts_ms, account, window, used_percent, credits_balance
         FROM limit_samples WHERE tool = ?1 AND kind = 'reading' AND credits_balance IS NOT NULL
         ORDER BY ts_ms",
    )?;
    // Accounts resolve first, so a reading naming none and one naming its
    // account at the same moment make one point.
    let mut by_account: BTreeMap<String, Vec<CreditPoint>> = BTreeMap::new();
    let mut rows = stmt.query(params![tool])?;
    while let Some(r) = rows.next()? {
        let ts: i64 = r.get(0)?;
        let raw: Option<String> = r.get(1)?;
        let Some((_, account)) = plans.resolve(raw.as_deref(), ts) else {
            continue;
        };
        let pct: f64 = r.get(3)?;
        let (session, weekly) = if r.get::<_, String>(2)? == "weekly" {
            (None, Some(pct))
        } else {
            (Some(pct), None)
        };
        let balance: f64 = r.get(4)?;
        let points = by_account.entry(account).or_default();
        let max = |a: Option<f64>, b: Option<f64>| match (a, b) {
            (Some(a), Some(b)) => Some(a.max(b)),
            (a, b) => a.or(b),
        };
        match points.last_mut() {
            Some(p) if p.0 == ts => {
                *p = (ts, max(p.1, session), max(p.2, weekly), p.3.max(balance));
            }
            _ => points.push((ts, session, weekly, balance)),
        }
    }
    let mut total: Option<(CreditUse, i64, i64)> = None;
    let mut funded = Funded::new();
    let mut acc = Acc::default();
    for (account, points) in by_account {
        let (Some(first), Some(last)) = (points.first(), points.last()) else {
            continue;
        };
        let (first_ms, last_ms) = (first.0, last.0);
        let no_resets = Vec::new();
        let (u, spans) = credit_use(&points, resets.get(&account).unwrap_or(&no_resets));
        let id = data.account_id(&account);
        let scope = Scope {
            models: None,
            account: Some(id),
            tier: None,
        };
        for r in spans.iter().flat_map(|&(a, b)| data.between(a, b)) {
            if scope.covers(r) {
                acc.add(r);
            }
        }
        funded.insert(id, spans);
        total = Some(match total {
            None => (u, first_ms, last_ms),
            Some((t, f, l)) => (
                CreditUse {
                    first_at: if first_ms < f { u.first_at } else { t.first_at },
                    last_at: if last_ms > l { u.last_at } else { t.last_at },
                    first_balance: t.first_balance + u.first_balance,
                    last_balance: t.last_balance + u.last_balance,
                    spent: t.spent + u.spent,
                    spent_at_limit: t.spent_at_limit + u.spent_at_limit,
                    spent_near_resets: t.spent_near_resets + u.spent_near_resets,
                    added: t.added + u.added,
                    credit_funded: Tokens::default(),
                },
                f.min(first_ms),
                l.max(last_ms),
            ),
        });
    }
    Ok(total.map(|(mut u, ..)| {
        u.credit_funded = acc.t;
        (u, funded)
    }))
}

/// One account's balance changes, and the spans paid with credits past
/// the limit. `points` is in time order and not empty.
fn credit_use(points: &[CreditPoint], resets: &[i64]) -> (CreditUse, Spans) {
    let (first, last) = (points[0], points[points.len() - 1]);
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
    (use_, spans)
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

/// One window of one tier: the plan's window, or a pool's.
struct WindowSpec<'a> {
    tier: usize,
    window: &'static str,
    pool: Option<&'a str>,
    /// The pool's models; `None` for every model.
    models: Option<&'a BTreeSet<u32>>,
    /// Readings no account could be told for, counted on this window.
    unattributed: i64,
}

/// One tier's window, every seat's readings measured on their own and the
/// runs pooled. Also returns each account's mid-window resets.
fn window_estimate(
    spec: &WindowSpec,
    plans: &Plans,
    readings: &Readings,
    data: &Data,
    now_ms: i64,
    tz: &impl TimeZone,
) -> (WindowEstimate, Vec<(String, Vec<i64>)>) {
    let len = window_ms(spec.window);
    let streams = readings.of(spec.tier, spec.window, spec.pool.unwrap_or(""));
    let scope_of = |account: &str| Scope {
        models: spec.models,
        account: Some(data.account_id(account)),
        tier: Some(spec.tier),
    };
    let mut runs = Vec::new();
    let mut by_month: BTreeMap<String, (Vec<Capacity>, Acc)> = BTreeMap::new();
    let mut model_costs: BTreeMap<u32, Vec<f64>> = BTreeMap::new();
    let mut peaks = Vec::new();
    let mut resets = Vec::new();
    let mut reset_count = 0;
    for &(account, samples) in &streams {
        let scope = scope_of(account);
        for (start, end, from, to, readings) in runs_of(samples, len) {
            let rise = to - from;
            if rise < MIN_RISE_PERCENT {
                continue;
            }
            let reqs = data.between(start, end);
            let acc = Acc::of(reqs, &scope);
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
            for r in reqs.iter().filter(|r| scope.covers(r)) {
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
        let r = mid_window_resets(samples);
        reset_count += r.len();
        peaks.extend(window_peaks(samples, len, &r, now_ms));
        resets.push((account.to_owned(), r));
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
    let accounts = readings.accounts(spec.tier);
    let lower_bound = if runs.is_empty() {
        // The busiest window of any one seat (seats never share a window),
        // over the requests that count toward this tier, read or not.
        let ids: BTreeSet<u32> = data
            .reqs
            .iter()
            .filter(|r| r.tier == Some(spec.tier))
            .filter_map(|r| r.account)
            .collect();
        ids.into_iter()
            .filter_map(|a| {
                let scope = Scope {
                    models: spec.models,
                    account: Some(a),
                    tier: Some(spec.tier),
                };
                busiest_window(data, &scope, len)
            })
            .max_by(|a, b| a.tokens.total_cmp(&b.tokens))
    } else {
        None
    };
    let all = || streams.iter().flat_map(|s| s.1.iter());
    let newest = |samples: &mut dyn Iterator<Item = &Sample>| {
        samples
            .filter(|s| !s.rejected)
            .max_by_key(|s| s.ts_ms)
            .map(|s| (s.ts_ms, s.used_percent, s.resets_at_ms, s.live))
    };
    let mut latest_by_seat: Vec<SeatLatest> = streams
        .iter()
        .filter_map(|&(account, samples)| {
            let (at, used_percent, resets_at, live) = newest(&mut samples.iter())?;
            let seat = plans.seat_of(spec.tier, account);
            Some(SeatLatest {
                seat: seat.and_then(|s| s.seat),
                account: (!account.is_empty()).then(|| account.to_owned()),
                label: seat.and_then(|s| s.plan.label.clone()),
                at: rfc3339(at),
                used_percent,
                resets_at: resets_at.map(rfc3339),
                live,
            })
        })
        .collect();
    latest_by_seat.sort_by_key(|l| l.seat.unwrap_or(i64::MAX));
    let utilization = utilization(&peaks, len, reset_count, tz);
    (
        WindowEstimate {
            plan: plans.tiers[spec.tier].0.clone(),
            seats: accounts.len().max(1) as i64,
            window: spec.window.to_owned(),
            window_hours: len / 3_600_000,
            readings: all().count() as i64,
            qualifying_runs: qualifying,
            spread: spread(&runs),
            runs,
            per_window,
            drift,
            by_model,
            lower_bound,
            pool: spec.pool.map(str::to_owned),
            models: spec
                .models
                .map(|m| m.iter().map(|&i| data.models[i as usize].clone()).collect())
                .unwrap_or_default(),
            live_readings: all().filter(|s| s.live).count() as i64,
            first_reading: all().map(|s| s.ts_ms).min().map(rfc3339),
            last_reading: all().map(|s| s.ts_ms).max().map(rfc3339),
            utilization,
            latest: newest(&mut all()).map(|(at, used_percent, resets_at, live)| Latest {
                at: rfc3339(at),
                used_percent,
                resets_at: resets_at.map(rfc3339),
                live,
            }),
            latest_by_seat,
            unattributed_readings: spec.unattributed,
        },
        resets,
    )
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
    plans: &Plans,
    funded: &Funded,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Vec<MonthValue> {
    // (usage, credit-funded cost, plan-covered use not paid with credits)
    let mut by_month: BTreeMap<String, (Acc, Option<f64>, Covered)> = BTreeMap::new();
    for r in &data.reqs {
        let m = by_month.entry(month_key(r.ts, tz)).or_default();
        m.0.add(r);
        if is_funded(funded, r) {
            *m.1.get_or_insert(0.0) += r.cost.unwrap_or(0.0);
        } else {
            m.2.add(r);
        }
    }
    let current = month_key(now_ms, tz);
    let first_key = by_month.keys().next().cloned();
    let first_ts = data.reqs.first().map_or(now_ms, |r| r.ts);
    by_month
        .into_iter()
        .map(|(month, (acc, funded_cost, plan_cost))| {
            let complete = month != current && Some(&month) != first_key.as_ref();
            let bounds = month_bounds(&month, tz);
            let (days_covered, days) = match bounds {
                Some((start, end)) => (
                    (end.min(now_ms) - start.max(first_ts)).max(0) as f64 / DAY_MS as f64,
                    (end - start) as f64 / DAY_MS as f64,
                ),
                None => (0.0, MONTH_DAYS),
            };
            let (price, paid) = bounds.map_or((None, None), |(a, b)| plans.price_over(a, b));
            let cost = acc.t.estimated_cost_usd;
            let covered = plan_cost.cost(cost.is_some());
            MonthValue {
                complete,
                multiple: match (covered, price) {
                    (Some(c), Some(p)) if p > 0.0 => Some(c / p),
                    _ => None,
                },
                price_usd: price,
                paid_usd: paid,
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

/// The estimated cost of plan-covered requests not paid with credits.
#[derive(Default)]
struct Covered {
    any: bool,
    cost: Option<f64>,
}

impl Covered {
    fn add(&mut self, r: &Req) {
        if r.covered {
            self.any = true;
            if let Some(c) = r.cost {
                *self.cost.get_or_insert(0.0) += c;
            }
        }
    }

    /// Null when covered use has no price; $0 when there is none at all
    /// but other use is priced (`priced`).
    fn cost(&self, priced: bool) -> Option<f64> {
        if self.any {
            self.cost
        } else {
            priced.then_some(0.0)
        }
    }
}

fn pace(data: &Data, plans: &Plans, funded: &Funded, now_ms: i64) -> Option<Pace> {
    let first = data.reqs.first()?.ts;
    let from = first.max(now_ms - PACE_DAYS * DAY_MS);
    let days = (now_ms - from) as f64 / DAY_MS as f64;
    if days < 2.0 {
        return None;
    }
    let (mut covered, mut priced) = (Covered::default(), false);
    for r in data.between(from - 1, now_ms) {
        if !is_funded(funded, r) {
            covered.add(r);
            priced |= r.cost.is_some();
        }
    }
    let cost = covered.cost(priced);
    let monthly = cost.map(|c| c / days * MONTH_DAYS);
    let (price, paid) = plans.price_over(from, now_ms);
    let priced_plans = plans
        .seats
        .iter()
        .filter(|s| s.plan.monthly_usd.is_some_and(|p| p > 0.0) && s.overlap(from, now_ms) > 0)
        .count() as i64;
    Some(Pace {
        days,
        monthly_cost_usd: monthly,
        multiple: match (monthly, price) {
            (Some(c), Some(p)) if p > 0.0 => Some(c / p),
            _ => None,
        },
        price_usd: price,
        paid_usd: paid,
        plans: priced_plans,
    })
}

/// The whole report: every known tool, plus any other with requests or readings.
pub fn report(
    conn: &Connection,
    subscriptions: &BTreeMap<String, Vec<Plan>>,
    rates: Option<&RatesFile>,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Result<ValueReport> {
    let mut tools: BTreeSet<String> = BTreeSet::new();
    for sql in [
        "SELECT DISTINCT tool FROM requests",
        "SELECT DISTINCT tool FROM limit_samples",
        "SELECT DISTINCT tool FROM live_limits WHERE lower(pool) != 'claude & gpt'",
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
    let sum = |f: &dyn Fn(&ToolValue) -> Option<f64>| {
        out.iter()
            .filter_map(f)
            .fold(None, |a: Option<f64>, c| Some(a.unwrap_or(0.0) + c))
    };
    let monthly_usd = sum(&|t| t.subscription.monthly_usd.filter(|p| *p > 0.0)).unwrap_or(0.0);
    let paid_monthly_usd = sum(&|t| {
        t.subscription
            .paid_monthly_usd
            .filter(|_| t.subscription.monthly_usd.is_some_and(|p| p > 0.0))
    })
    .unwrap_or(0.0);
    // The pace sums run over the tools whose plans had a price over those
    // days and whose use there has an estimated cost, so cost and price
    // cover the same tools (see `paced`).
    let pace_cost = sum(&|t| paced(t)?.monthly_cost_usd);
    let pace_price = sum(&|t| paced(t)?.price_usd);
    let pace_paid = sum(&|t| paced(t)?.paid_usd);
    let over = |p: Option<f64>| match (pace_cost, p) {
        (Some(c), Some(p)) if p > 0.0 => Some(c / p),
        _ => None,
    };
    Ok(ValueReport {
        generated_at: rfc3339(now_ms),
        min_rise_percent: MIN_RISE_PERCENT,
        runs_averaged: RUNS_AVERAGED as i64,
        pace_days: PACE_DAYS,
        combined: Combined {
            plans: out
                .iter()
                .flat_map(|t| &t.subscription.plans)
                .filter(|p| p.active && p.monthly_usd.is_some_and(|p| p > 0.0))
                .count() as i64,
            monthly_usd,
            paid_monthly_usd,
            pace_plans: out.iter().filter_map(|t| Some(paced(t)?.plans)).sum(),
            pace_price_usd: pace_price,
            pace_paid_usd: pace_paid,
            pace_monthly_cost_usd: pace_cost,
            pace_multiple: over(pace_price),
            pace_paid_multiple: over(pace_paid),
        },
        tools: out,
    })
}

fn tool_value(
    conn: &Connection,
    tool: &str,
    subscriptions: &BTreeMap<String, Vec<Plan>>,
    rates: Option<&RatesFile>,
    now_ms: i64,
    tz: &impl TimeZone,
) -> Result<ToolValue> {
    let plans = Plans::load(conn, tool, subscriptions, tz)?;
    let mut data = Data::load(conn, tool, &plans)?;
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
             WHEN EXISTS (SELECT 1 FROM live_limits WHERE tool = ?1 AND lower(pool) != 'claude & gpt') THEN 'live'
             WHEN EXISTS (SELECT 1 FROM limit_samples WHERE tool = ?1) THEN 'rejections'
             ELSE 'none' END",
        params![tool],
        |r| r.get(0),
    )?;
    let readings = Readings::load(conn, tool, &plans)?;
    // (pool, window) of every pool window read, sessions first.
    let mut pools: Vec<(String, &'static str)> = readings
        .streams
        .keys()
        .map(|k| (k.3.clone(), k.2))
        .chain(readings.unattributed.keys().map(|k| (k.1.clone(), k.0)))
        .filter(|p| !p.0.is_empty())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    pools.sort_by(|a, b| (&a.0, a.1 == "weekly").cmp(&(&b.0, b.1 == "weekly")));
    let pool_names: Vec<String> = pools
        .iter()
        .map(|p| p.0.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let pool_ids: BTreeMap<&str, BTreeSet<u32>> = pool_names
        .iter()
        .map(|p| (p.as_str(), pool_models(p, &pool_names, &data.models)))
        .collect();
    // Active local tiers come first (read or not) so the tool's main window
    // stays on a local current plan, followed by active remote tiers, then
    // ended tiers with readings, then future tiers with readings; with none of
    // those, the primary tier stands in.
    let home = plans.primary_tier(now_ms);
    let running_local = |t: &usize| {
        plans
            .seats
            .iter()
            .any(|s| s.tier == *t && !s.plan.remote && s.active_at(now_ms))
    };
    let running_any = |t: &usize| {
        plans
            .seats
            .iter()
            .any(|s| s.tier == *t && s.active_at(now_ms))
    };
    let started = |t: &usize| {
        plans
            .seats
            .iter()
            .any(|s| s.tier == *t && s.from_ms <= now_ms)
    };
    let has_readings = |t: &usize| readings.streams.keys().any(|k| k.0 == *t);
    let mut tiers: Vec<usize> = (0..plans.tiers.len()).filter(running_local).collect();
    tiers.extend((0..plans.tiers.len()).filter(|t| !running_local(t) && running_any(t)));
    tiers.extend(
        (0..plans.tiers.len()).filter(|t| !running_any(t) && started(t) && has_readings(t)),
    );
    tiers.extend(
        (0..plans.tiers.len()).filter(|t| !running_any(t) && !started(t) && has_readings(t)),
    );
    if tiers.is_empty() {
        tiers.push(home);
    }
    let attach = if tiers.contains(&home) {
        home
    } else {
        tiers[0]
    };
    let unattributed = |tier: usize, window: &str, pool: &str| {
        if tier != attach {
            return 0;
        }
        readings
            .unattributed
            .iter()
            .filter(|(k, _)| k.0 == window && k.1 == pool)
            .map(|(_, n)| *n)
            .sum()
    };
    let mut specs: Vec<WindowSpec> = Vec::new();
    for &tier in &tiers {
        for window in ["session", "weekly"] {
            specs.push(WindowSpec {
                tier,
                window,
                pool: None,
                models: None,
                unattributed: unattributed(tier, window, ""),
            });
        }
        for (pool, window) in &pools {
            let n = unattributed(tier, window, pool);
            if n > 0 || !readings.of(tier, window, pool).is_empty() {
                specs.push(WindowSpec {
                    tier,
                    window,
                    pool: Some(pool.as_str()),
                    models: pool_ids.get(pool.as_str()),
                    unattributed: n,
                });
            }
        }
    }
    let mut windows = Vec::new();
    let mut resets: HashMap<String, Vec<i64>> = HashMap::new();
    for spec in &specs {
        let (w, r) = window_estimate(spec, &plans, &readings, &data, now_ms, tz);
        if spec.pool.is_none() {
            for (account, r) in r {
                resets.entry(account).or_default().extend(r);
            }
        }
        windows.push(w);
    }
    for r in resets.values_mut() {
        r.sort_unstable();
    }
    let session_shares = session_shares(conn, tool, &plans, &readings, &pool_ids, &data)?;
    let (credits, funded) = match credits(conn, tool, &plans, &resets, &data)? {
        Some((c, spans)) => (Some(c), spans),
        None => (None, Funded::new()),
    };
    let months = months(&data, &plans, &funded, now_ms, tz);
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
        pace: pace(&data, &plans, &funded, now_ms),
        credits,
        session_shares,
        months,
        unplanned_accounts: unplanned_accounts(&plans, &readings, now_ms),
        subscription: plans.subscription(now_ms),
    })
}

/// Accounts read in the last `PACE_DAYS` days at a time no plan line names
/// them. With no plan naming any account, only when there are several.
fn unplanned_accounts(plans: &Plans, readings: &Readings, now_ms: i64) -> Vec<String> {
    let since = now_ms - PACE_DAYS * DAY_MS;
    let mut recent: BTreeMap<&str, bool> = BTreeMap::new();
    let read = readings
        .streams
        .iter()
        .flat_map(|((_, a, ..), samples)| samples.iter().map(move |s| (a.as_str(), s.ts_ms)))
        .chain(readings.off_plan.iter().map(|(a, ts)| (a.as_str(), *ts)));
    for (account, ts) in read.filter(|(a, ts)| !a.is_empty() && *ts >= since) {
        let named = plans
            .seats
            .iter()
            .any(|p| p.plan.account.as_deref() == Some(account) && p.active_at(ts));
        *recent.entry(account).or_default() |= !named;
    }
    let any_named = plans.seats.iter().any(|s| s.plan.account.is_some());
    if !any_named && recent.len() < 2 {
        return Vec::new();
    }
    recent
        .into_iter()
        .filter(|(_, unplanned)| *unplanned)
        .map(|(a, _)| a.to_owned())
        .collect()
}

/// Pair each session window with the weekly window it counts toward: the
/// weekly window of its own pool, or the plan's. A weekly pool with no
/// session window of its own (Claude's Fable) pairs with the plan's session,
/// which also fills with other models, so each window's session rise is cut
/// to the pool's share of that window's estimated cost: the figure is for a
/// session spent on the pool's models alone. Each account's windows are
/// read on their own, paired by its own pools, and pooled by plan.
fn session_shares(
    conn: &Connection,
    tool: &str,
    plans: &Plans,
    readings: &Readings,
    pool_ids: &BTreeMap<&str, BTreeSet<u32>>,
    data: &Data,
) -> Result<Vec<SessionShare>> {
    // (tier, account) -> the pool windows it has readings of.
    let mut read: BTreeMap<(usize, &str), BTreeSet<(&str, &str)>> = BTreeMap::new();
    for (tier, account, window, pool) in readings.streams.keys() {
        let pools = read.entry((*tier, account.as_str())).or_default();
        if !pool.is_empty() {
            pools.insert((pool.as_str(), *window));
        }
    }
    let pairs_of = |pools: &BTreeSet<(&str, &str)>| {
        let has = |pool: &str, window: &str| pools.contains(&(pool, window));
        let mut pairs = BTreeSet::from([(String::new(), String::new())]);
        for &(pool, window) in pools {
            if window == "session" {
                let weekly = if has(pool, "weekly") { pool } else { "" };
                pairs.insert((pool.to_owned(), weekly.to_owned()));
            } else if !has(pool, "session") {
                pairs.insert((String::new(), pool.to_owned()));
            }
        }
        pairs
    };
    let stream_pairs: BTreeMap<(usize, &str), BTreeSet<(String, String)>> =
        read.iter().map(|(k, p)| (*k, pairs_of(p))).collect();
    let none = pairs_of(&BTreeSet::new());
    let pairs: BTreeSet<(String, String)> = stream_pairs
        .values()
        .flatten()
        .chain(&none)
        .cloned()
        .collect();
    let mut out = Vec::new();
    for (sp, wp) in pairs {
        let mut stmt = conn.prepare_cached(
            "SELECT s.ts_ms, s.used_percent, s.resets_at_ms, w.used_percent, w.resets_at_ms, s.account
                 FROM limit_samples s JOIN limit_samples w
                 ON w.tool = s.tool AND w.source_id = s.source_id AND w.ts_ms = s.ts_ms
                    AND w.window = 'weekly' AND w.kind = 'reading'
                 WHERE s.tool = ?1 AND s.window = 'session' AND s.kind = 'reading'
                    AND ?2 = '' AND ?3 = ''
             UNION ALL
             SELECT s.ts_ms, s.used_percent, s.resets_at_ms, w.used_percent, w.resets_at_ms, s.account
                 FROM live_limits s JOIN live_limits w
                 ON w.tool = s.tool AND w.account = s.account AND w.ts_ms = s.ts_ms
                    AND w.window = 'weekly' AND w.pool = ?3
                 WHERE s.tool = ?1 AND s.window = 'session' AND s.pool = ?2
             ORDER BY 1",
        )?;
        let mut by_stream: BTreeMap<(usize, String), Vec<PairedReading>> = BTreeMap::new();
        let mut rows = stmt.query(params![tool, sp, wp])?;
        while let Some(r) = rows.next()? {
            let ts: i64 = r.get(0)?;
            let raw: Option<String> = r.get(5)?;
            let Some(key) = plans.resolve(raw.as_deref(), ts) else {
                continue;
            };
            let paired = stream_pairs
                .get(&(key.0, key.1.as_str()))
                .unwrap_or(&none)
                .contains(&(sp.clone(), wp.clone()));
            if paired && !plans.off_plan(&key.1, ts) {
                by_stream.entry(key).or_default().push(PairedReading {
                    ts,
                    session: r.get(1)?,
                    session_reset: r.get(2)?,
                    weekly: r.get(3)?,
                    weekly_reset: r.get(4)?,
                });
            }
        }
        let mut by_tier: BTreeMap<usize, (i64, f64, f64)> = BTreeMap::new();
        for ((tier, account), rows) in &by_stream {
            let account = Some(data.account_id(account));
            let sums = if sp.is_empty() && !wp.is_empty() {
                let scope = Scope {
                    models: pool_ids.get(wp.as_str()),
                    account,
                    tier: Some(*tier),
                };
                share_sums(rows, |start, end| pool_cost_share(data, &scope, start, end))
            } else {
                share_sums(rows, |_, _| Some(1.0))
            };
            let t = by_tier.entry(*tier).or_default();
            *t = (t.0 + sums.0, t.1 + sums.1, t.2 + sums.2);
        }
        for (tier, (windows, session, weekly)) in by_tier {
            out.push(SessionShare {
                plan: plans.tiers[tier].0.clone(),
                session_pool: (!sp.is_empty()).then(|| sp.clone()),
                weekly_pool: (!wp.is_empty()).then(|| wp.clone()),
                windows,
                weekly_percent: (session > 0.0 && windows >= SHARE_MIN_WINDOWS)
                    .then(|| 100.0 * weekly / session),
            });
        }
    }
    Ok(out)
}

/// The pool's share of the estimated cost of one account's requests from
/// `start` to `end`; null when none of them is priced.
fn pool_cost_share(data: &Data, scope: &Scope, start: i64, end: i64) -> Option<f64> {
    let from = data.reqs.partition_point(|r| r.ts < start);
    let to = data.reqs.partition_point(|r| r.ts <= end);
    let (mut pool, mut all) = (0.0, 0.0);
    for r in data.reqs[from..to].iter().filter(|r| scope.of_stream(r)) {
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

/// (windows, weekly percent per full session) from one account's paired
/// readings; see `share_sums`.
#[cfg(test)]
fn share_of(
    rows: &[PairedReading],
    weight: impl Fn(i64, i64) -> Option<f64>,
) -> (i64, Option<f64>) {
    let (n, session, weekly) = share_sums(rows, weight);
    (n, (session > 0.0).then(|| 100.0 * weekly / session))
}

/// (windows, summed session rise, summed weekly rise) from one account's
/// time-ordered paired readings. Readings are grouped by their two reset
/// times, so a reset bought mid-window starts a new group; the rise of each
/// share is its highest reading less its lowest, which also absorbs stale
/// readings from concurrent sessions. `weight(start, end)` scales a window's
/// session rise to the part spent on the weekly window's models; a null
/// weight drops it.
fn share_sums(rows: &[PairedReading], weight: impl Fn(i64, i64) -> Option<f64>) -> (i64, f64, f64) {
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
    (
        rises.len() as i64,
        rises.iter().map(|r| r.0).sum(),
        rises.iter().map(|r| r.1).sum(),
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

/// The RFC 3339 time `days` before `rfc`.
pub fn days_before(rfc: &str, days: f64) -> Option<String> {
    let t = chrono::DateTime::parse_from_rfc3339(rfc).ok()?;
    Some(rfc3339(
        t.timestamp_millis() - (days * DAY_MS as f64) as i64,
    ))
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

/// A tool's pace when it counts toward the combined figures: its plans had
/// a list price over the pace's days, and its use there an estimated cost.
pub fn paced(t: &ToolValue) -> Option<&Pace> {
    t.pace
        .as_ref()
        .filter(|p| p.price_usd.is_some_and(|p| p > 0.0) && p.monthly_cost_usd.is_some())
}

/// The tool's leading window of a kind: a running plan's when any runs.
/// When every plan has ended, `windows[]` leads with the first ended tier
/// with readings (else the first plan), and that stands in.
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
    /// Accounts no plan line names. Private: it names the accounts.
    Accounts,
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
        let pays_list = (c.monthly_usd - c.paid_monthly_usd).abs() < 0.005;
        let mut s = format!(
            "Over the last {} days, the same use at API list prices would cost est. {} a month. The {} plans {} {} a month",
            r.pace_days,
            dollars(cost),
            c.plans,
            if pays_list { "cost" } else { "list at" },
            dollars(c.monthly_usd),
        );
        if !pays_list {
            s.push_str(&format!(" (you pay {})", dollars(c.paid_monthly_usd)));
        }
        let pace_price = c.pace_price_usd.unwrap_or(c.monthly_usd);
        if (pace_price - c.monthly_usd).abs() >= 0.5 {
            s.push_str(&format!(
                "; over those days they averaged {}",
                dollars(pace_price)
            ));
            if let Some(paid) = c.pace_paid_usd.filter(|_| !pays_list) {
                s.push_str(&format!(" ({} paid)", dollars(paid)));
            }
        }
        if pays_list {
            s.push_str(&format!(", so that is {x:.1}x their price."));
        } else {
            s.push_str(&format!(", so that is {x:.1}x their list price"));
            match c.pace_paid_multiple {
                Some(y) => s.push_str(&format!(" and {y:.1}x what you pay.")),
                None => s.push('.'),
            }
        }
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
        if let Some(p) = &t.pace
            && let (Some(cost), Some(x), Some(price)) =
                (p.monthly_cost_usd, p.multiple, p.price_usd)
        {
            let paid = p.paid_usd.filter(|paid| (paid - price).abs() >= 0.005);
            if t.subscription.plans.len() == 1 {
                s.push_str(&format!(
                    " Pace: est. {} a month, {:.1}x the {} plan",
                    dollars(cost),
                    x,
                    dollars(price)
                ));
                if let Some(paid) = paid {
                    s.push_str(&format!(" ({} paid)", dollars(paid)));
                }
            } else {
                s.push_str(&format!(
                    " Pace: est. {} a month, {:.1}x the {} list price of its plans",
                    dollars(cost),
                    x,
                    dollars(price)
                ));
                if let Some(paid) = paid {
                    s.push_str(&format!(" ({} paid)", dollars(paid)));
                }
            }
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
        let multi_plan = t
            .windows
            .iter()
            .filter(|w| w.pool.is_none() && w.window == "session")
            .count()
            > 1
            || t.windows
                .iter()
                .any(|x| t.windows.first().is_some_and(|first| x.plan != first.plan));
        type PoolGroup<'a> = (
            usize,
            &'a str,
            &'a str,
            Option<&'a WindowEstimate>,
            Option<&'a WindowEstimate>,
        );
        let mut pool_groups: Vec<PoolGroup<'_>> = Vec::new();
        let mut tier_idx = 0usize;
        for (i, w) in t.windows.iter().enumerate() {
            if i > 0 && w.pool.is_none() && w.window == "session" {
                tier_idx += 1;
            }
            if w.pool.is_none() || w.per_window.is_none() {
                continue;
            }
            let pool = w.pool.as_deref().unwrap_or("");
            let entry = match pool_groups
                .iter_mut()
                .find(|(ti, p, n, ..)| *ti == tier_idx && *p == w.plan.as_str() && *n == pool)
            {
                Some(e) => e,
                None => {
                    pool_groups.push((tier_idx, w.plan.as_str(), pool, None, None));
                    pool_groups.last_mut().unwrap()
                }
            };
            if w.window == "weekly" {
                entry.4 = Some(w);
            } else {
                entry.3 = Some(w);
            }
        }
        for (_, plan, pool, session_w, weekly_w) in pool_groups {
            let prefix = if multi_plan {
                format!("{} ({plan}) {pool} pool", t.tool)
            } else {
                format!("{} {pool} pool", t.tool)
            };
            let line = match (session_w, weekly_w) {
                (Some(sw), Some(ww)) => format!(
                    "{prefix}: est. {} per 5-hour window, est. {} per weekly window.",
                    sw.per_window
                        .as_ref()
                        .and_then(|c| c.estimated_cost_usd)
                        .map_or("-".into(), dollars),
                    ww.per_window
                        .as_ref()
                        .and_then(|c| c.estimated_cost_usd)
                        .map_or("-".into(), dollars),
                ),
                (Some(w), None) | (None, Some(w)) => {
                    let c = w.per_window.as_ref().unwrap();
                    format!(
                        "{prefix}: est. {} per full {} window ({} tokens).",
                        c.estimated_cost_usd.map_or("-".into(), dollars),
                        window_words(w),
                        short_tokens(c.tokens),
                    )
                }
                (None, None) => continue,
            };
            out.push((Finding::Pool, line));
        }
        // Windows at their limit right now, from live readings; with
        // several seats, each seat's own window.
        for w in &t.windows {
            if w.latest_by_seat.len() > 1 {
                for (i, l) in w.latest_by_seat.iter().enumerate() {
                    // Only a seat's own live reading says it is full now.
                    if l.used_percent >= EXHAUSTED_PERCENT && l.live {
                        let seat = format!("seat {}", l.seat.unwrap_or(i as i64 + 1));
                        let latest = Latest {
                            at: l.at.clone(),
                            used_percent: l.used_percent,
                            resets_at: l.resets_at.clone(),
                            live: l.live,
                        };
                        out.push((Finding::Now, latest_words(t, w, Some(&seat), &latest)));
                    }
                }
            } else if w
                .latest
                .as_ref()
                .is_some_and(|l| l.used_percent >= EXHAUSTED_PERCENT)
                && let Some(l) = latest_sentence(t, w)
            {
                out.push((Finding::Now, l));
            }
        }
        if !t.unplanned_accounts.is_empty() {
            out.push((
                Finding::Accounts,
                format!(
                    "{} has readings from {} that no [subscriptions] line names ({}); add a line with account= for each seat so its price counts.",
                    t.tool,
                    match t.unplanned_accounts.len() {
                        1 => "an account".to_owned(),
                        n => format!("{n} accounts"),
                    },
                    t.unplanned_accounts.join(", ")
                ),
            ));
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
    let l = w.latest.as_ref().filter(|l| l.live)?;
    Some(latest_words(t, w, None, l))
}

fn latest_words(t: &ToolValue, w: &WindowEstimate, seat: Option<&str>, l: &Latest) -> String {
    let mut s = format!(
        "{} {}{}: {:.0} % used on {}",
        t.tool,
        window_label(w),
        seat.map(|s| format!(", {s}")).unwrap_or_default(),
        l.used_percent,
        date_of(&l.at)
    );
    if let Some(r) = &l.resets_at {
        s.push_str(&format!(", resets {}", date_of(r)));
    }
    s.push('.');
    s
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
        let sub = &t.subscription;
        let _ = writeln!(
            s,
            "{}: {} at {}/month{}; limit readings: {}{}",
            t.tool,
            sub.name,
            usd(sub.monthly_usd),
            if sub.paid_monthly_usd == sub.monthly_usd {
                String::new()
            } else {
                format!(" (you pay {})", usd(sub.paid_monthly_usd))
            },
            t.limit_source,
            match &t.reference_model {
                Some(m) => format!("; reference model {m}"),
                None => String::new(),
            }
        );
        let plain = |p: &PlanValue| {
            p.account.is_none()
                && p.label.is_none()
                && p.from.is_none()
                && p.until.is_none()
                && !p.remote
        };
        if sub.plans.len() > 1 || sub.plans.iter().any(|p| !plain(p)) {
            for p in &sub.plans {
                let mut tags: Vec<String> = p.label.iter().chain(&p.account).cloned().collect();
                if p.remote {
                    tags.push("used on another machine".into());
                }
                let _ = writeln!(
                    s,
                    "  plan: {}{}{}: {} list, {} paid{}{}{}",
                    p.name,
                    p.seat.map(|n| format!(", seat {n}")).unwrap_or_default(),
                    if tags.is_empty() {
                        String::new()
                    } else {
                        format!(" [{}]", tags.join(", "))
                    },
                    usd(p.monthly_usd),
                    usd(p.paid_monthly_usd),
                    p.from
                        .as_ref()
                        .map(|f| format!(", from {f}"))
                        .unwrap_or_default(),
                    p.until
                        .as_ref()
                        .map(|u| format!(", until {u}"))
                        .unwrap_or_default(),
                    if p.active { "" } else { " (not running now)" },
                );
            }
        }
        if !t.unplanned_accounts.is_empty() {
            let _ = writeln!(
                s,
                "  accounts no plan line names: {}",
                t.unplanned_accounts.join(", ")
            );
        }
        let tiers: BTreeSet<&str> = t.windows.iter().map(|w| w.plan.as_str()).collect();
        for w in &t.windows {
            let tier = if tiers.len() > 1 || w.seats > 1 {
                format!("{}, ", tier_label(&w.plan, w.seats))
            } else {
                String::new()
            };
            let _ = write!(s, "  {tier}{}: ", window_label(w));
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
            if w.latest_by_seat.len() > 1 {
                for (i, l) in w.latest_by_seat.iter().enumerate() {
                    let _ = writeln!(
                        s,
                        "    seat {}{}: {:.0} % at {}",
                        l.seat.unwrap_or(i as i64 + 1),
                        match l.label.as_ref().or(l.account.as_ref()) {
                            Some(n) => format!(" ({n})"),
                            None => String::new(),
                        },
                        l.used_percent,
                        l.at
                    );
                }
            }
            if w.unattributed_readings > 0 {
                let _ = writeln!(
                    s,
                    "    {} readings left out: they name no account, and several were in use",
                    w.unattributed_readings
                );
            }
        }
        for m in &t.months {
            let repriced = m.price_usd != sub.monthly_usd || m.paid_usd != m.price_usd;
            let _ = writeln!(
                s,
                "  {}{}: est. {}; {} the price while a plan ran{}{}",
                m.month,
                if m.complete { "" } else { " (partial)" },
                usd(m.usage.estimated_cost_usd),
                match m.multiple {
                    Some(x) => format!("{x:.1}x"),
                    None => "-".into(),
                },
                if repriced {
                    format!(" ({} list, {} paid)", usd(m.price_usd), usd(m.paid_usd))
                } else {
                    String::new()
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
                "  mean over {}: est. {}; {:.1}x the price while a plan ran",
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
                "  pace over the last {:.0} days: est. {} a month = {:.1}x the price{}",
                p.days,
                usd(Some(c)),
                x,
                match (p.price_usd, p.paid_usd) {
                    (Some(l), Some(paid)) if (l - paid).abs() >= 0.005 && paid > 0.0 => format!(
                        " ({} list); {:.1}x the {} paid",
                        usd(Some(l)),
                        c / paid,
                        usd(Some(paid))
                    ),
                    (Some(l), _) if Some(l) != sub.monthly_usd =>
                        format!(" ({} list)", usd(Some(l))),
                    _ => String::new(),
                }
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
            live: false,
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
            live: false,
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
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'session', {pct}, {}, 'reading', NULL, NULL);",
                june + ts,
                june + reset
            ));
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'weekly', {}, NULL, 'reading', NULL, NULL);",
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
            "INSERT INTO limit_samples VALUES (2, 'claude-code', 'c', {}, 'session', 100, {}, 'rejected', NULL, NULL);",
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
        // Both windows peaked between 25 and 75 %, early reset or not.
        assert_eq!(u.peak_bands, [0, 2, 0, 0]);
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
        assert!(text.contains("2026-06 (partial): est. $1.30; 0.1x the price while a plan ran"));
        assert!(text.contains("antigravity: Google AI Pro at $19.99/month; limit readings: none"));
    }

    #[test]
    fn configured_subscriptions_replace_defaults() {
        let store = fixture();
        let mut subs = BTreeMap::new();
        subs.insert(
            "codex".to_owned(),
            vec![Plan {
                name: "ChatGPT Pro".into(),
                monthly_usd: Some(200.0),
                paid_monthly_usd: Some(200.0),
                ..default_subscription("codex")
            }],
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
                live: false,
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
    fn each_seat_keeps_its_own_windows_credits_and_price() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let at = |d: u32, h: u32| {
            Utc.with_ymd_and_hms(2026, 11, d, h, 0, 0)
                .unwrap()
                .timestamp_millis()
        };
        let (a, b) = ("aaaaaaaaaaaa", "bbbbbbbbbbbb");
        let mut sql = format!(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'codex', '/l1');
             INSERT INTO sessions (tool, session_id, account) VALUES ('codex', 'a', '{a}'), ('codex', 'b', '{b}');"
        );
        let mut reading = |ts: i64,
                           window: &str,
                           pct: f64,
                           reset: Option<i64>,
                           bal: &str,
                           acc: &str| {
            let reset = reset.map_or("NULL".into(), |r| r.to_string());
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {ts}, '{window}', {pct}, {reset}, 'reading', {bal}, {acc});"
            ));
        };
        // Before seat b starts, a reading naming no account is seat a's.
        reading(at(10, 0), "session", 5.0, None, "NULL", "NULL");
        // On the 20th both seats fill at once against different reset times:
        // interleaved, neither would show a run.
        let d = at(20, 0);
        for (i, pct) in [0.0, 25.0, 50.0].into_iter().enumerate() {
            let ts = d + i as i64 * HOUR;
            reading(
                ts,
                "session",
                pct,
                Some(d + 5 * HOUR),
                "NULL",
                &format!("'{a}'"),
            );
            reading(
                ts + 1000,
                "session",
                pct + 10.0,
                Some(d + 4 * HOUR),
                "NULL",
                &format!("'{b}'"),
            );
        }
        // Now two accounts run, so a reading naming none is left out.
        reading(
            d + 3 * HOUR,
            "session",
            70.0,
            Some(d + 5 * HOUR),
            "NULL",
            "NULL",
        );
        // Seat a pays credits at its weekly limit; seat b's requests then are its own.
        reading(
            d + 3 * HOUR,
            "weekly",
            100.0,
            Some(d + 100 * HOUR),
            "10",
            &format!("'{a}'"),
        );
        reading(
            d + 4 * HOUR,
            "weekly",
            100.0,
            Some(d + 100 * HOUR),
            "8",
            &format!("'{a}'"),
        );
        for (k, ts, session, cost) in [
            (0, at(1, 1), "a", 0.5),
            (1, d + 90 * 60_000, "a", 1.0),
            (2, d + 90 * 60_000, "b", 3.0),
            (3, d + 210 * 60_000, "a", 1.0),
            (4, d + 210 * 60_000, "b", 1.0),
        ] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('codex', 'r{k}', '{session}', 1, {ts}, 'm', 100, 0, {cost});"
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let line = |extra: &str| crate::config::parse(&format!("[subscriptions]\n{extra}"));
        let subs = line(&format!(
            "codex = 20 \"ChatGPT Plus\" account={a}\ncodex = 20 \"ChatGPT Plus\" paid=0 account={b} from=2026-11-16"
        ))
        .unwrap()
        .subscriptions;
        let r = report(&store.conn, &subs, None, at(30, 0) + DAY_MS, &Utc).unwrap();
        let t = r.tools.iter().find(|t| t.tool == "codex").unwrap();
        assert_eq!(
            (
                t.subscription.name.as_str(),
                t.subscription.monthly_usd,
                t.subscription.paid_monthly_usd
            ),
            ("ChatGPT Plus \u{d7} 2", Some(40.0), Some(20.0))
        );
        let w = main_window(t, "session").unwrap();
        assert_eq!((w.seats, w.readings, w.unattributed_readings), (2, 7, 1));
        let mut costs: Vec<f64> = w
            .runs
            .iter()
            .filter_map(|r| r.per_window.estimated_cost_usd)
            .collect();
        costs.sort_by(f64::total_cmp);
        assert_eq!(costs, vec![2.0, 6.0]);
        assert_eq!(w.utilization.as_ref().unwrap().windows, 2);
        assert_eq!(w.latest_by_seat.len(), 2);
        let c = t.credits.as_ref().unwrap();
        assert_eq!((c.spent_at_limit, c.credit_funded.requests), (2.0, 1));
        // Seat b ran 15 of November's 30 days (from the 16th) and is not paid for.
        let nov = &t.months[0];
        assert_eq!(nov.credit_funded_cost_usd, Some(1.0));
        assert!((nov.price_usd.unwrap() - (20.0 + 20.0 * 15.0 / 30.0)).abs() < 1e-9);
        assert_eq!(nov.paid_usd, Some(20.0));
        let pace = t.pace.as_ref().unwrap();
        assert!((pace.price_usd.unwrap() - (20.0 + 20.0 * 15.0 / 28.0)).abs() < 1e-9);
        assert!(t.unplanned_accounts.is_empty());
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
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'session', {pct}, {}, 'reading', {bal}, NULL);",
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
                "INSERT INTO live_limits VALUES ('claude-code', '', 'Fable', 'weekly', {}, {pct}, {reset});",
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
            plan: "ChatGPT Plus".into(),
            seats: 1,
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
            latest_by_seat: vec![],
            unattributed_readings: 0,
        };
        let mut t = ToolValue {
            tool: "codex".into(),
            subscription: Plans::new(vec![default_subscription("codex")], BTreeSet::new(), &Utc)
                .subscription(0),
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
            unplanned_accounts: vec![],
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
    fn an_account_that_changes_tier_keeps_each_tiers_requests_apart() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let at = |d: u32, h: u32| {
            Utc.with_ymd_and_hms(2026, 11, d, h, 0, 0)
                .unwrap()
                .timestamp_millis()
        };
        let a = "aaaaaaaaaaaa";
        let mut sql = format!(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'codex', '/l1');
             INSERT INTO sessions (tool, session_id, account) VALUES ('codex', 'a', '{a}');"
        );
        // On Plus: 0 -> 50 % over two hours. Pro, from the 10th, is never read.
        for (h, pct) in [(0, 0.0), (2, 50.0)] {
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {}, 'session', {pct}, {}, 'reading', NULL, '{a}');",
                at(5, h),
                at(5, 5)
            ));
        }
        for (k, ts, tokens, cost) in [
            (0, at(5, 1), 100, 1.0),
            (1, at(15, 1), 1000, 5.0),
            (2, at(15, 2), 1000, 5.0),
        ] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('codex', 'r{k}', 'a', 1, {ts}, 'm', {tokens}, 0, {cost});"
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let subs = crate::config::parse(&format!(
            "[subscriptions]\ncodex = 20 \"ChatGPT Plus\" account={a} until=2026-11-09\ncodex = 200 \"ChatGPT Pro\" account={a} from=2026-11-10"
        ))
        .unwrap()
        .subscriptions;
        let r = report(&store.conn, &subs, None, at(20, 0), &Utc).unwrap();
        let t = r.tools.iter().find(|t| t.tool == "codex").unwrap();
        let names: Vec<_> = t
            .windows
            .iter()
            .map(|w| format!("{} {}", w.plan, w.window))
            .collect();
        assert_eq!(
            names,
            [
                "ChatGPT Pro session",
                "ChatGPT Pro weekly",
                "ChatGPT Plus session",
                "ChatGPT Plus weekly"
            ]
        );
        // The current plan leads, measured on its own requests only.
        let pro = main_window(t, "session").unwrap();
        assert_eq!(pro.plan, "ChatGPT Pro");
        assert!(pro.per_window.is_none());
        let bound = pro.lower_bound.as_ref().unwrap();
        assert_eq!(
            (bound.tokens, bound.estimated_cost_usd),
            (2000.0, Some(10.0))
        );
        let said = format!("{:?}", tagged_findings(&r));
        assert!(
            said.contains("the busiest held at least est. $10."),
            "{said}"
        );
        // Plus keeps its run, and Pro's requests never reach its windows.
        let plus = &t.windows[2];
        assert_eq!(plus.runs[0].usage.requests, 1);
        let week = t.windows[3].lower_bound.as_ref().unwrap();
        assert_eq!((week.tokens, week.requests), (100.0, 1.0));
        // Plus retired within the pace days, so both plans stand behind the pace.
        assert_eq!(
            (t.pace.as_ref().unwrap().plans, r.combined.pace_plans),
            (2, 2)
        );
    }

    #[test]
    fn retired_seat_requests_do_not_count_toward_a_tier_another_seat_keeps() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = Utc
            .with_ymd_and_hms(2026, 11, 10, 0, 0, 0)
            .unwrap()
            .timestamp_millis();
        let (a, b) = ("aaaaaaaaaaaa", "bbbbbbbbbbbb");
        let mut sql = format!(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'codex', '/l1');
             INSERT INTO sessions (tool, session_id, account) VALUES ('codex', 'a', '{a}'), ('codex', 'b', '{b}');"
        );
        // Seat a retires on the 12th; its later request must not count.
        for (k, ts, session, tokens) in [
            (0, t0, "a", 100),
            (1, t0 + HOUR, "b", 300),
            (2, t0 + 3 * DAY_MS, "a", 1000),
        ] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('codex', 'r{k}', '{session}', 1, {ts}, 'm', {tokens}, 0, 1.0);"
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let subs = crate::config::parse(&format!(
            "[subscriptions]\ncodex = 20 \"ChatGPT Plus\" account={a} until=2026-11-12\ncodex = 20 \"ChatGPT Plus\" account={b}"
        ))
        .unwrap()
        .subscriptions;
        let r = report(&store.conn, &subs, None, t0 + 5 * DAY_MS, &Utc).unwrap();
        let t = r.tools.iter().find(|t| t.tool == "codex").unwrap();
        let w = main_window(t, "session").unwrap();
        assert_eq!(w.lower_bound.as_ref().unwrap().tokens, 300.0);
    }

    #[test]
    fn retired_unnamed_fallback_seat_does_not_borrow_active_named_seat_dates() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = Utc
            .with_ymd_and_hms(2026, 11, 10, 0, 0, 0)
            .unwrap()
            .timestamp_millis();
        let (b, c) = ("bbbbbbbbbbbb", "cccccccccccc");
        let mut sql = format!(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'codex', '/l1');
             INSERT INTO sessions (tool, session_id, account) VALUES ('codex', 'b', '{b}'), ('codex', 'c', '{c}');"
        );
        // Unconfigured account c falls back to the unnamed seat, which retires on
        // the 12th; its later request must not borrow named seat b's open span.
        for (k, ts, session, tokens) in [
            (0, t0, "c", 100),
            (1, t0 + HOUR, "b", 300),
            (2, t0 + 3 * DAY_MS, "c", 1000),
        ] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('codex', 'r{k}', '{session}', 1, {ts}, 'm', {tokens}, 0, 1.0);"
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let subs = crate::config::parse(&format!(
            "[subscriptions]\ncodex = 20 \"ChatGPT Plus\" until=2026-11-12\ncodex = 20 \"ChatGPT Plus\" account={b}"
        ))
        .unwrap()
        .subscriptions;
        let r = report(&store.conn, &subs, None, t0 + 5 * DAY_MS, &Utc).unwrap();
        let t = r.tools.iter().find(|t| t.tool == "codex").unwrap();
        let w = main_window(t, "session").unwrap();
        assert_eq!(w.lower_bound.as_ref().unwrap().tokens, 300.0);
    }

    #[test]
    fn busiest_window_is_per_seat_and_only_a_seats_live_reading_is_current() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = Utc
            .with_ymd_and_hms(2026, 11, 10, 0, 0, 0)
            .unwrap()
            .timestamp_millis();
        let now = t0 + 5 * DAY_MS;
        let (a, b) = ("aaaaaaaaaaaa", "bbbbbbbbbbbb");
        let mut sql = format!(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'codex', '/l1');
             INSERT INTO sessions (tool, session_id, account) VALUES ('codex', 'a', '{a}'), ('codex', 'b', '{b}');"
        );
        for (k, ts, session, tokens) in [
            (0, t0, "a", 100),
            (1, t0 + HOUR, "b", 300),
            (2, t0 + 2 * HOUR, "a", 100),
        ] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('codex', 'r{k}', '{session}', 1, {ts}, 'm', {tokens}, 0, 1.0);"
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let subs = crate::config::parse(&format!(
            "[subscriptions]\ncodex = 20 \"ChatGPT Plus\" account={a}\ncodex = 20 \"ChatGPT Plus\" account={b}"
        ))
        .unwrap()
        .subscriptions;
        let bound = |store: &crate::store::Store| {
            let r = report(&store.conn, &subs, None, now, &Utc).unwrap();
            let t = r.tools.iter().find(|t| t.tool == "codex").unwrap();
            let w = main_window(t, "session").unwrap();
            w.lower_bound.as_ref().unwrap().tokens
        };
        // Seat b's busiest window, never the two seats added together.
        assert_eq!(bound(&store), 300.0);
        // Seat a's old full reading, and seat b's live one: b still counts,
        // and a's old reading is not a limit reached now.
        store
            .conn
            .execute_batch(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {t0}, 'session', 100, {}, 'reading', NULL, '{a}');
                 INSERT INTO live_limits VALUES ('codex', '{b}', '', 'session', {}, 20, {});",
                t0 + 5 * HOUR,
                now - HOUR,
                now + HOUR
            ))
            .unwrap();
        assert_eq!(bound(&store), 300.0);
        let now_findings = |store: &crate::store::Store| {
            let r = report(&store.conn, &subs, None, now, &Utc).unwrap();
            tagged_findings(&r)
                .into_iter()
                .filter(|f| f.0 == Finding::Now)
                .map(|f| f.1)
                .collect::<Vec<_>>()
        };
        assert_eq!(now_findings(&store), Vec::<String>::new());
        store
            .conn
            .execute_batch(&format!(
                "INSERT INTO live_limits VALUES ('codex', '{a}', '', 'session', {}, 100, {});",
                now - HOUR,
                now + HOUR
            ))
            .unwrap();
        assert_eq!(
            now_findings(&store),
            ["codex session window (5 h), seat 1: 100 % used on 2026-11-14, resets 2026-11-15."]
        );
    }

    /// UTC-5, then UTC-4 from 2026-03-08 07:00 UTC: 02:00-03:00 local
    /// that day never happens.
    #[derive(Clone, Copy)]
    struct SpringForward;

    impl TimeZone for SpringForward {
        type Offset = chrono::FixedOffset;
        fn from_offset(_: &Self::Offset) -> Self {
            SpringForward
        }
        fn offset_from_local_date(
            &self,
            _: &chrono::NaiveDate,
        ) -> chrono::MappedLocalTime<Self::Offset> {
            unimplemented!()
        }
        fn offset_from_local_datetime(
            &self,
            local: &NaiveDateTime,
        ) -> chrono::MappedLocalTime<Self::Offset> {
            let jump = chrono::NaiveDate::from_ymd_opt(2026, 3, 8)
                .unwrap()
                .and_hms_opt(2, 0, 0)
                .unwrap();
            let h = |n| chrono::FixedOffset::west_opt(n * 3600).unwrap();
            if *local < jump {
                chrono::MappedLocalTime::Single(h(5))
            } else if *local < jump + chrono::Duration::hours(1) {
                chrono::MappedLocalTime::None
            } else {
                chrono::MappedLocalTime::Single(h(4))
            }
        }
        fn offset_from_utc_date(&self, _: &chrono::NaiveDate) -> Self::Offset {
            unimplemented!()
        }
        fn offset_from_utc_datetime(&self, utc: &NaiveDateTime) -> Self::Offset {
            let jump = chrono::NaiveDate::from_ymd_opt(2026, 3, 8)
                .unwrap()
                .and_hms_opt(7, 0, 0)
                .unwrap();
            chrono::FixedOffset::west_opt(if *utc < jump { 5 } else { 4 } * 3600).unwrap()
        }
    }

    #[test]
    fn a_skipped_local_time_reads_as_the_clock_change() {
        let at = |h, m| {
            chrono::NaiveDate::from_ymd_opt(2026, 3, 8)
                .unwrap()
                .and_hms_opt(h, m, 0)
                .unwrap()
        };
        let jump = Utc
            .with_ymd_and_hms(2026, 3, 8, 7, 0, 0)
            .unwrap()
            .timestamp_millis();
        assert_eq!(local_ms(at(2, 30), &SpringForward), jump);
        assert_eq!(local_ms(at(1, 59), &SpringForward), jump - 60_000);
        assert_eq!(local_ms(at(3, 0), &SpringForward), jump);
    }

    #[test]
    fn dollars_and_tokens_read_short() {
        assert_eq!(dollars(1296.71), "$1,297");
        assert_eq!(dollars(9.4), "$9");
        assert_eq!(short_tokens(3_199_200_000.0), "3.2B");
        assert_eq!(short_tokens(29_234_515.0), "29.2M");
        assert_eq!(short_tokens(45_000.0), "45k");
    }

    #[test]
    fn excluded_claude_and_gpt_pool_is_omitted_from_report() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = 1_792_000_000_000i64;
        let reset = t0 + 5 * HOUR;
        let mut sql = String::from(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'antigravity', '/l1');
             INSERT INTO sessions (tool, session_id) VALUES ('antigravity', 'a');",
        );
        for (ts, pct) in [(0, 10.0), (HOUR, 60.0)] {
            sql.push_str(&format!(
                "INSERT INTO live_limits VALUES ('antigravity', '', 'Claude & GPT', 'session', {}, {pct}, {reset});
                 INSERT INTO live_limits VALUES ('antigravity', '', 'Claude & GPT', 'weekly', {}, {pct}, {});",
                t0 + ts,
                t0 + ts,
                t0 + 7 * 24 * HOUR
            ));
        }
        sql.push_str(&format!(
            "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, canonical_model, input_tokens, output_tokens, cost_usd)
             VALUES ('antigravity', 'r1', 'a', 1, {}, 'claude-opus-5-5', 'claude-opus-5-5', 1000, 0, 2.0);",
            t0 + 30 * 60_000
        ));
        store.conn.execute_batch(&sql).unwrap();

        let r = report(&store.conn, &BTreeMap::new(), None, t0 + 2 * HOUR, &Utc).unwrap();
        let ag = r.tools.iter().find(|t| t.tool == "antigravity").unwrap();
        assert_eq!(ag.limit_source, "none");
        assert!(ag.windows.iter().all(|w| w.pool.is_none()));
        assert!(
            ag.session_shares
                .iter()
                .all(|s| s.session_pool.is_none() && s.weekly_pool.is_none())
        );
        assert!(!findings(&r).iter().any(|f| f.contains("Claude & GPT")));

        // Add Gemini and Argon readings: both remain while Claude & GPT stays out.
        let week_reset = t0 + 7 * 24 * HOUR;
        store
            .conn
            .execute_batch(&format!(
                "INSERT INTO live_limits VALUES
                    ('antigravity', '', 'Gemini', 'session', {t0}, 10.0, {reset}),
                    ('antigravity', '', 'Gemini', 'session', {}, 60.0, {reset}),
                    ('antigravity', '', 'Argon', 'weekly', {t0}, 10.0, {week_reset}),
                    ('antigravity', '', 'Argon', 'weekly', {}, 60.0, {week_reset});
                 INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, canonical_model, input_tokens, output_tokens, cost_usd)
                 VALUES
                    ('antigravity', 'r2', 'a', 1, {}, 'gemini-4-carbon', 'gemini-4-carbon', 1000, 0, 5.0),
                    ('antigravity', 'r3', 'a', 1, {}, 'gemini-4-argon', 'gemini-4-argon', 500, 0, 1.0);",
                t0 + HOUR,
                t0 + HOUR,
                t0 + 30 * 60_000,
                t0 + 40 * 60_000
            ))
            .unwrap();
        let r = report(&store.conn, &BTreeMap::new(), None, t0 + 2 * HOUR, &Utc).unwrap();
        let ag = r.tools.iter().find(|t| t.tool == "antigravity").unwrap();
        assert_eq!(ag.limit_source, "live");
        let pools: BTreeSet<&str> = ag
            .windows
            .iter()
            .filter_map(|w| w.pool.as_deref())
            .collect();
        assert_eq!(pools, BTreeSet::from(["Argon", "Gemini"]));
        let pool_findings: Vec<_> = tagged_findings(&r)
            .into_iter()
            .filter(|(k, _)| *k == Finding::Pool)
            .map(|(_, s)| s)
            .collect();
        assert!(pool_findings.iter().any(|f| f.contains("Gemini pool")));
        assert!(pool_findings.iter().any(|f| f.contains("Argon pool")));
        assert!(!pool_findings.iter().any(|f| f.contains("Claude & GPT")));
    }

    #[test]
    fn local_active_tier_leads_remote_active_tier() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let at = |d: u32, h: u32| {
            Utc.with_ymd_and_hms(2026, 11, d, h, 0, 0)
                .unwrap()
                .timestamp_millis()
        };
        let (pro, ultra) = ("111122223333", "444455556666");
        let mut sql = "INSERT INTO sources (id, tool, path) VALUES (1, 'antigravity', '/l1');
             INSERT INTO sessions (tool, session_id) VALUES ('antigravity', 's1');"
            .to_string();
        for (k, ts, cost) in [(0, at(5, 1), 10.0), (1, at(15, 1), 25.0)] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('antigravity', 'r{k}', 's1', 1, {ts}, 'gemini-4-carbon', 1000, 0, {cost});"
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let subs = crate::config::parse(&format!(
            "[subscriptions]
antigravity = 19.99 \"Google AI Pro\" account={pro} until=2026-11-09
antigravity = 19.99 \"Google AI Pro\" account={pro} from=2026-11-10 remote
antigravity = 200 \"Google AI Ultra\" paid=0 account={ultra} from=2026-11-10"
        ))
        .unwrap()
        .subscriptions;
        let r = report(&store.conn, &subs, None, at(18, 0), &Utc).unwrap();
        let ag = r.tools.iter().find(|t| t.tool == "antigravity").unwrap();
        assert_eq!(main_window(ag, "session").unwrap().plan, "Google AI Ultra");
        assert_eq!(main_window(ag, "weekly").unwrap().plan, "Google AI Ultra");
        assert_eq!(
            main_window(ag, "session")
                .unwrap()
                .lower_bound
                .as_ref()
                .unwrap()
                .estimated_cost_usd,
            Some(25.0)
        );
    }

    #[test]
    fn excluded_pool_account_does_not_make_other_pools_ambiguous() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = 1_792_000_000_000i64;
        let reset = t0 + 5 * HOUR;
        let second = "bbbbbbbbbbbb";
        store
            .conn
            .execute_batch(&format!(
                "INSERT INTO sources (id, tool, path) VALUES (1, 'antigravity', '/l1');
                 INSERT INTO sessions (tool, session_id) VALUES ('antigravity', 'a');
                 INSERT INTO live_limits VALUES
                    ('antigravity', '{second}', 'claude & gpt', 'session', {t0}, 20.0, {reset}),
                    ('antigravity', '', 'Gemini', 'session', {t0}, 10.0, {reset}),
                    ('antigravity', '', 'Gemini', 'session', {}, 60.0, {reset});
                 INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, canonical_model, input_tokens, output_tokens, cost_usd)
                 VALUES ('antigravity', 'r1', 'a', 1, {}, 'gemini-4-carbon', 'gemini-4-carbon', 1000, 0, 5.0);",
                t0 + HOUR,
                t0 + 30 * 60_000
            ))
            .unwrap();
        let r = report(&store.conn, &BTreeMap::new(), None, t0 + 2 * HOUR, &Utc).unwrap();
        let ag = r.tools.iter().find(|t| t.tool == "antigravity").unwrap();
        let gemini = ag
            .windows
            .iter()
            .find(|w| w.pool.as_deref() == Some("Gemini"))
            .unwrap();
        assert_eq!(gemini.unattributed_readings, 0);
        assert_eq!(
            gemini.per_window.as_ref().unwrap().estimated_cost_usd,
            Some(10.0)
        );
    }

    #[test]
    fn same_name_tiers_keep_separate_pool_findings() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = 1_792_000_000_000i64;
        let reset = t0 + 5 * HOUR;
        let (a, b) = ("aaaaaaaaaaaa", "bbbbbbbbbbbb");
        store
            .conn
            .execute_batch(&format!(
                "INSERT INTO sources (id, tool, path) VALUES (1, 'antigravity', '/l1');
                 INSERT INTO sessions (tool, session_id, account) VALUES
                    ('antigravity', 'sa', '{a}'),
                    ('antigravity', 'sb', '{b}');
                 INSERT INTO live_limits VALUES
                    ('antigravity', '{a}', 'Gemini', 'session', {t0}, 10.0, {reset}),
                    ('antigravity', '{a}', 'Gemini', 'session', {}, 60.0, {reset}),
                    ('antigravity', '{b}', 'Gemini', 'session', {t0}, 10.0, {reset}),
                    ('antigravity', '{b}', 'Gemini', 'session', {}, 60.0, {reset});
                 INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, canonical_model, input_tokens, output_tokens, cost_usd)
                 VALUES
                    ('antigravity', 'ra', 'sa', 1, {}, 'gemini-4-carbon', 'gemini-4-carbon', 1000, 0, 5.0),
                    ('antigravity', 'rb', 'sb', 1, {}, 'gemini-4-carbon', 'gemini-4-carbon', 1000, 0, 15.0);",
                t0 + HOUR,
                t0 + HOUR,
                t0 + 30 * 60_000,
                t0 + 30 * 60_000
            ))
            .unwrap();
        let subs = crate::config::parse(&format!(
            "[subscriptions]
antigravity = 20 \"Google AI\" account={a}
antigravity = 200 \"Google AI\" account={b}"
        ))
        .unwrap()
        .subscriptions;
        let r = report(&store.conn, &subs, None, t0 + 2 * HOUR, &Utc).unwrap();
        let pool_lines: Vec<_> = tagged_findings(&r)
            .into_iter()
            .filter(|(k, _)| *k == Finding::Pool)
            .map(|(_, s)| s)
            .collect();
        assert_eq!(pool_lines.len(), 2, "{pool_lines:?}");
        assert!(
            pool_lines.iter().any(|l| l.contains("$10")),
            "{pool_lines:?}"
        );
        assert!(
            pool_lines.iter().any(|l| l.contains("$30")),
            "{pool_lines:?}"
        );
    }

    #[test]
    fn ended_tier_leads_future_tier_in_historical_fallback() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let at = |m: u32, d: u32, h: u32| {
            Utc.with_ymd_and_hms(2026, m, d, h, 0, 0)
                .unwrap()
                .timestamp_millis()
        };
        let (future, ended) = ("111122223333", "444455556666");
        store
            .conn
            .execute_batch(&format!(
                "INSERT INTO live_limits VALUES
                    ('antigravity', '{future}', '', 'session', {}, 15.0, NULL),
                    ('antigravity', '{ended}', '', 'session', {}, 25.0, NULL);",
                at(11, 5, 10),
                at(11, 5, 10)
            ))
            .unwrap();
        // Future tier listed first; both have readings, neither runs on Nov 18.
        let subs = crate::config::parse(&format!(
            "[subscriptions]
antigravity = 200 \"Future Plan\" account={future} from=2026-12-01
antigravity = 19.99 \"Ended Plan\" account={ended} until=2026-11-10"
        ))
        .unwrap()
        .subscriptions;
        let r = report(&store.conn, &subs, None, at(11, 18, 12), &Utc).unwrap();
        let ag = r.tools.iter().find(|t| t.tool == "antigravity").unwrap();
        assert_eq!(main_window(ag, "session").unwrap().plan, "Ended Plan");
        assert_eq!(main_window(ag, "weekly").unwrap().plan, "Ended Plan");
    }

    #[test]
    fn usage_after_a_plan_ends_measures_and_buys_nothing() {
        let r = after_plan_report("1.0");
        let t = r.tools.iter().find(|t| t.tool == "codex").unwrap();
        assert!(main_window(t, "session").unwrap().runs.is_empty());
        let nov = t.months.iter().find(|m| m.month == "2026-11").unwrap();
        assert_eq!(nov.usage.estimated_cost_usd, Some(101.0));
        // $1 of in-plan use over nine days of a $30 plan.
        assert!((nov.multiple.unwrap() - 1.0 / 9.0).abs() < 1e-9, "{nov:?}");
        // In-plan use with no price has no multiple, whatever came after.
        let r = after_plan_report("NULL");
        let t = r.tools.iter().find(|t| t.tool == "codex").unwrap();
        assert_eq!(t.months.last().unwrap().multiple, None);
        assert_eq!(t.pace.as_ref().unwrap().multiple, None);
    }

    /// A $30 plan ending on the 10th: one request in it at `in_plan_cost`,
    /// one $100 request after it, and readings either side of its end.
    fn after_plan_report(in_plan_cost: &str) -> ValueReport {
        let store = crate::store::Store::open_in_memory().unwrap();
        let at = |d: u32, h: u32, m: u32| {
            Utc.with_ymd_and_hms(2026, 11, d, h, m, 0)
                .unwrap()
                .timestamp_millis()
        };
        let a = "aaaaaaaaaaaa";
        // 0 % before the plan ends, 100 % after it, one request in the plan.
        let mut sql = format!(
            "INSERT INTO sources (id, tool, path) VALUES (1, 'codex', '/l1');
             INSERT INTO sessions (tool, session_id, account) VALUES ('codex', 'a', '{a}');"
        );
        for (ts, pct) in [(at(9, 23, 0), 0.0), (at(10, 1, 0), 100.0)] {
            sql.push_str(&format!(
                "INSERT INTO limit_samples VALUES (1, 'codex', 'a', {ts}, 'session', {pct}, {}, 'reading', NULL, '{a}');",
                at(10, 3, 0)
            ));
        }
        for (k, ts, cost) in [(0, at(9, 23, 30), in_plan_cost), (1, at(15, 1, 0), "100.0")] {
            sql.push_str(&format!(
                "INSERT INTO requests (tool, request_key, session_id, source_id, ts_ms, model, input_tokens, output_tokens, cost_usd)
                 VALUES ('codex', 'r{k}', 'a', 1, {ts}, 'm', 100, 0, {cost});"
            ));
        }
        store.conn.execute_batch(&sql).unwrap();
        let subs = crate::config::parse(&format!(
            "[subscriptions]\ncodex = 30 account={a} until=2026-11-10T00:00"
        ))
        .unwrap()
        .subscriptions;
        report(&store.conn, &subs, None, at(20, 0, 0), &Utc).unwrap()
    }

    #[test]
    fn one_accounts_pools_do_not_change_anothers_session_shares() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let t0 = 1_792_000_000_000i64;
        let (a, b) = ("aaaaaaaaaaaa", "bbbbbbbbbbbb");
        let week = t0 + 7 * DAY_MS;
        let mut sql =
            "INSERT INTO sources (id, tool, path) VALUES (1, 'antigravity', '/l1');".to_owned();
        // Account a: three Gemini session windows against the plan's weekly.
        for k in 0..3 {
            let start = t0 + k * 6 * HOUR;
            for (dt, session, weekly) in [(0, 10.0, 1.0), (HOUR, 60.0, 6.0)] {
                let (ts, reset) = (start + dt, start + 5 * HOUR);
                let weekly = weekly + 10.0 * k as f64;
                sql.push_str(&format!(
                    "INSERT INTO live_limits VALUES
                        ('antigravity', '{a}', 'Gemini', 'session', {ts}, {session}, {reset}),
                        ('antigravity', '{a}', '', 'weekly', {ts}, {weekly}, {week});"
                ));
            }
        }
        // Account b reads a Gemini weekly window of its own.
        sql.push_str(&format!(
            "INSERT INTO live_limits VALUES ('antigravity', '{b}', 'Gemini', 'weekly', {t0}, 5.0, {week});"
        ));
        store.conn.execute_batch(&sql).unwrap();
        let subs = crate::config::parse(&format!(
            "[subscriptions]\nantigravity = 20 \"Plus\" account={a}\nantigravity = 200 \"Pro\" account={b}"
        ))
        .unwrap()
        .subscriptions;
        let r = report(&store.conn, &subs, None, t0 + DAY_MS, &Utc).unwrap();
        let ag = r.tools.iter().find(|t| t.tool == "antigravity").unwrap();
        let share = ag
            .session_shares
            .iter()
            .find(|s| s.plan == "Plus" && s.session_pool.as_deref() == Some("Gemini"))
            .unwrap();
        assert_eq!((share.windows, share.weekly_pool.as_deref()), (3, None));
        assert!((share.weekly_percent.unwrap() - 10.0).abs() < 1e-9);
    }
}
