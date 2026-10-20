//! Usage data shared by every provider, plus the analysis drawn from it:
//! which windows are blocked and when a window runs out at its current pace.

use chrono::{DateTime, Utc};
use serde::Serialize;

/// A window with less than this much left counts as blocked.
pub const BLOCKED_BELOW_REMAINING: f64 = 3.0;

#[derive(Serialize)]
pub struct Window {
    pub label: String,
    pub used_percent: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resets_at: Option<DateTime<Utc>>,
    #[serde(rename = "window_seconds", skip_serializing_if = "Option::is_none")]
    pub window_secs: Option<i64>,
    /// The model family this window limits; `None` limits the whole provider.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pool: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    pub blocked: bool,
    /// When the window hits its limit at the current pace, if before the reset.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runs_out_at: Option<DateTime<Utc>>,
}

impl Window {
    pub fn new(
        label: impl Into<String>,
        used: f64,
        resets_at: Option<DateTime<Utc>>,
        window_secs: Option<i64>,
    ) -> Self {
        Window {
            label: label.into(),
            used_percent: used.clamp(0.0, 100.0),
            resets_at,
            window_secs,
            pool: None,
            detail: None,
            blocked: false,
            runs_out_at: None,
        }
    }

    pub fn pool(mut self, pool: impl Into<String>) -> Self {
        self.pool = Some(pool.into());
        self
    }

    pub fn detail(mut self, detail: impl Into<String>) -> Self {
        self.detail = Some(detail.into());
        self
    }

    pub fn remaining(&self) -> f64 {
        100.0 - self.used_percent
    }

    /// Linear projection from the usage so far in this window. Skipped early
    /// in a window, where a few requests would swing the estimate wildly.
    fn project_run_out(&self, now: DateTime<Utc>) -> Option<DateTime<Utc>> {
        let (reset, length) = (self.resets_at?, self.window_secs?);
        if self.blocked || self.used_percent <= 0.0 {
            return None;
        }
        let left = (reset - now).num_seconds();
        let elapsed = length - left;
        if left <= 0 || elapsed < (length / 50).max(600) {
            return None;
        }
        let secs_to_full = self.remaining() / (self.used_percent / elapsed as f64);
        (secs_to_full < left as f64).then(|| now + chrono::Duration::seconds(secs_to_full as i64))
    }
}

#[derive(Serialize, Default)]
pub struct Report {
    pub provider: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub plan: Option<String>,
    pub windows: Vec<Window>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub notes: Vec<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub advice: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl Report {
    /// Fills in the derived fields once the provider's data is in.
    pub fn finish(mut self, now: DateTime<Utc>) -> Self {
        for w in &mut self.windows {
            w.blocked = w.remaining() < BLOCKED_BELOW_REMAINING;
            w.runs_out_at = w.project_run_out(now);
        }
        self
    }
}

/// What a provider can be used for right now.
pub struct Status<'a> {
    /// e.g. "Claude", "Claude (not Fable)", "Antigravity (Gemini)"; `None` if nothing is usable.
    pub usable: Option<String>,
    pub blocked: Vec<&'a Window>,
    /// Highest usage among the windows that still allow use.
    pub worst_usable: Option<f64>,
    /// When something becomes usable again, if nothing is now.
    pub unblocks_at: Option<DateTime<Utc>>,
}

pub fn status(r: &Report) -> Status<'_> {
    let blocked: Vec<&Window> = r.windows.iter().filter(|w| w.blocked).collect();
    if r.error.is_some() || r.windows.is_empty() {
        return Status { usable: None, blocked, worst_usable: None, unblocks_at: None };
    }

    let mut pools: Vec<&str> = Vec::new();
    for p in r.windows.iter().filter_map(|w| w.pool.as_deref()) {
        if !pools.contains(&p) {
            pools.push(p);
        }
    }
    let pool_blocked = |p: &str| blocked.iter().any(|w| w.pool.as_deref() == Some(p));
    let (blocked_pools, open_pools): (Vec<&str>, Vec<&str>) = pools.iter().partition(|p| pool_blocked(p));
    let wide_blocked: Vec<&&Window> = blocked.iter().filter(|w| w.pool.is_none()).collect();
    let has_wide = r.windows.iter().any(|w| w.pool.is_none());

    let usable = if !wide_blocked.is_empty() {
        None
    } else if has_wide && blocked_pools.is_empty() {
        Some(r.provider.to_string())
    } else if has_wide {
        Some(format!("{} (not {})", r.provider, blocked_pools.join(", ")))
    } else if open_pools.is_empty() {
        None
    } else {
        Some(format!("{} ({})", r.provider, open_pools.join(", ")))
    };

    let worst_usable = usable.as_ref().and_then(|_| {
        r.windows
            .iter()
            .filter(|w| !w.blocked && w.pool.as_deref().is_none_or(|p| open_pools.contains(&p)))
            .map(|w| w.used_percent)
            .reduce(f64::max)
    });

    // Every blocking window in a group has to reset before the group opens.
    let all_reset = |ws: &mut dyn Iterator<Item = &&Window>| -> Option<DateTime<Utc>> {
        ws.map(|w| w.resets_at).collect::<Option<Vec<_>>>()?.into_iter().max()
    };
    let unblocks_at = match () {
        _ if usable.is_some() => None,
        _ if !wide_blocked.is_empty() => all_reset(&mut wide_blocked.iter().copied()),
        _ => blocked_pools
            .iter()
            .filter_map(|p| all_reset(&mut blocked.iter().filter(|w| w.pool.as_deref() == Some(*p))))
            .min(),
    };

    Status { usable, blocked, worst_usable, unblocks_at }
}

/// Time from now until `t`: "45m", "3h 10m", "4d 18h".
pub fn until(t: DateTime<Utc>) -> String {
    span((t - Utc::now()).num_seconds())
}

/// Time since `t`, in the same form.
pub fn ago(t: DateTime<Utc>) -> String {
    span((Utc::now() - t).num_seconds())
}

fn span(secs: i64) -> String {
    let mins = (secs.max(0) + 59) / 60;
    let (d, h, m) = (mins / 1440, mins % 1440 / 60, mins % 60);
    match (d, h) {
        (0, 0) => format!("{m}m"),
        (0, _) => format!("{h}h {m}m"),
        _ => format!("{d}d {h}h"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    const HOUR: i64 = 3600;

    /// A finished window `elapsed_h` hours into a `len_h`-hour window.
    fn win(label: &str, used: f64, elapsed_h: i64, len_h: i64) -> Window {
        Window::new(label, used, Some(Utc::now() + Duration::hours(len_h - elapsed_h)), Some(len_h * HOUR))
    }

    fn report(windows: Vec<Window>) -> Report {
        Report { provider: "P", windows, ..Default::default() }.finish(Utc::now())
    }

    #[test]
    fn blocked_below_three_percent_remaining() {
        let r = report(vec![win("a", 97.0, 1, 5), win("b", 97.5, 1, 5)]);
        assert!(!r.windows[0].blocked);
        assert!(r.windows[1].blocked);
    }

    #[test]
    fn projects_run_out_only_when_before_reset() {
        // 50% used 2h into a 5h window: 100% at 4h, before the 5h reset.
        let r = report(vec![win("fast", 50.0, 2, 5), win("slow", 20.0, 2, 5)]);
        let out = r.windows[0].runs_out_at.expect("projected");
        let hours_left = (out - Utc::now()).num_minutes() as f64 / 60.0;
        assert!((hours_left - 2.0).abs() < 0.05, "{hours_left}");
        assert!(r.windows[1].runs_out_at.is_none());
    }

    #[test]
    fn no_projection_early_in_window_or_when_blocked() {
        let early = Window::new("early", 30.0, Some(Utc::now() + Duration::minutes(295)), Some(5 * HOUR));
        let r = report(vec![early, win("blocked", 99.0, 1, 5)]);
        assert!(r.windows.iter().all(|w| w.runs_out_at.is_none()));
    }

    #[test]
    fn provider_wide_block_makes_nothing_usable() {
        let r = report(vec![win("Session", 100.0, 4, 5), win("Weekly", 10.0, 24, 168)]);
        let s = status(&r);
        assert!(s.usable.is_none());
        assert_eq!(s.blocked.len(), 1);
        assert!(s.unblocks_at.is_some());
    }

    #[test]
    fn blocked_scoped_pool_is_excluded() {
        let r = report(vec![win("Weekly", 10.0, 24, 168), win("Weekly · Fable", 99.0, 24, 168).pool("Fable")]);
        let s = status(&r);
        assert_eq!(s.usable.as_deref(), Some("P (not Fable)"));
        assert_eq!(s.worst_usable, Some(10.0));
    }

    #[test]
    fn independent_pools_list_the_open_ones() {
        let r = report(vec![
            win("Gemini · Weekly", 82.0, 48, 168).pool("Gemini"),
            win("Claude & GPT · Session", 100.0, 3, 5).pool("Claude & GPT"),
        ]);
        let s = status(&r);
        assert_eq!(s.usable.as_deref(), Some("P (Gemini)"));
        assert_eq!(s.worst_usable, Some(82.0));
    }

    #[test]
    fn all_pools_blocked_unblocks_at_earliest_pool() {
        let r = report(vec![
            win("A", 100.0, 3, 5).pool("A"),
            win("B", 100.0, 24, 168).pool("B"),
        ]);
        let s = status(&r);
        assert!(s.usable.is_none());
        let hours = (s.unblocks_at.unwrap() - Utc::now()).num_minutes() as f64 / 60.0;
        assert!((hours - 2.0).abs() < 0.05, "{hours}");
    }
}
