//! URL handling: the query string becomes a [`Params`], the store becomes a
//! [`Page`], and the page becomes HTML. Kept free of the HTTP crate so tests
//! can render straight from a `Connection`.

use super::data::{self, Window};
use super::html::{self, Page};
use anyhow::Result;
use chrono::{Local, Offset, TimeZone};
use rusqlite::Connection;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Params {
    pub window: Option<Window>,
    pub tool: Option<&'static str>,
}

impl Params {
    /// Parse `window=12w&tool=codex`. Unknown keys and values are ignored.
    pub fn parse(query: &str) -> Params {
        let mut p = Params::default();
        for pair in query.split('&') {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            match k {
                "window" => p.window = Some(Window::parse(Some(v))),
                "tool" => p.tool = data::parse_tool(Some(v)),
                _ => {}
            }
        }
        p
    }
}

/// "Tue 20 Oct 2026, 00:56" in `tz`.
pub fn clock_label(ms: i64, tz: &impl TimeZone) -> String {
    tz.timestamp_millis_opt(ms)
        .single()
        .map(|t| t.fixed_offset().format("%a %-d %b %Y, %H:%M").to_string())
        .unwrap_or_default()
}

/// "28 May 2026" in `tz`.
pub fn date_label(ms: i64, tz: &impl TimeZone) -> String {
    tz.timestamp_millis_opt(ms)
        .single()
        .map(|t| t.fixed_offset().format("%-d %b %Y").to_string())
        .unwrap_or_default()
}

/// "UTC-04:00" for the zone's offset at `ms`.
pub fn tz_label(ms: i64, tz: &impl TimeZone) -> String {
    let Some(t) = tz.timestamp_millis_opt(ms).single() else {
        return "UTC".into();
    };
    let secs = t.offset().fix().local_minus_utc();
    if secs == 0 {
        return "UTC".into();
    }
    let sign = if secs < 0 { '-' } else { '+' };
    let secs = secs.abs();
    format!("UTC{sign}{:02}:{:02}", secs / 3600, (secs % 3600) / 60)
}

/// Load everything the page needs from an open store.
pub fn load_page(
    conn: &Connection,
    params: Params,
    now_ms: i64,
    tz: &impl TimeZone,
    rates_fetched_at: Option<String>,
) -> Result<Page> {
    let started = std::time::Instant::now();
    let window = params.window.unwrap_or(Window::FourWeeks);
    let since = window.since_ms(now_ms);
    let facts = data::store_facts(conn)?;
    let heatmap = data::heatmap(conn, since, params.tool, tz)?;
    let totals = data::totals(conn, since, params.tool)?;
    Ok(Page {
        window,
        tool: params.tool,
        now_label: clock_label(now_ms, tz),
        tz_label: tz_label(now_ms, tz),
        first_label: facts.first_ts_ms.map(|t| date_label(t, tz)),
        facts,
        heatmap,
        totals,
        rates_fetched_at,
        load_ms: started.elapsed().as_secs_f64() * 1000.0,
    })
}

/// Render the dashboard for a query string, in the system's local zone.
pub fn render_dashboard(
    conn: &Connection,
    query: &str,
    now_ms: i64,
    rates_fetched_at: Option<String>,
) -> Result<String> {
    let page = load_page(conn, Params::parse(query), now_ms, &Local, rates_fetched_at)?;
    Ok(html::render(&page))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::FixedOffset;

    #[test]
    fn params_parse() {
        assert_eq!(Params::parse(""), Params::default());
        assert_eq!(
            Params::parse("window=12w&tool=codex&x=1"),
            Params {
                window: Some(Window::TwelveWeeks),
                tool: Some("codex")
            }
        );
        assert_eq!(
            Params::parse("tool=nope&window=all"),
            Params {
                window: Some(Window::All),
                tool: None
            }
        );
    }

    fn fixture() -> crate::store::Store {
        let store = crate::store::Store::open_in_memory().unwrap();
        store
            .conn
            .execute_batch(
                "INSERT INTO sessions (tool, session_id, title, cwd) VALUES
                     ('codex', 's1', 'Fix <the> build & \"ship\"', '/home/demo/proj');
                 INSERT INTO requests (tool, request_key, session_id, ts_ms, model,
                     input_tokens, cache_read_input_tokens, output_tokens, cost_usd) VALUES
                     ('codex', 'r1', 's1', 1792380600000, 'm', 100, 1000, 50, 0.25),
                     ('codex', 'r2', 's1', 1792384200000, 'm', 200, 2000, 80, NULL),
                     ('claude-code', 'r3', 's1', 1792387800000, 'm', 300, 3000, 90, 1.5);",
            )
            .unwrap();
        store
    }

    fn assert_offline(html: &str) {
        assert!(!html.contains("http://"), "page references http://");
        assert!(!html.contains("https://"), "page references https://");
        assert!(!html.contains("<link"), "page links an outside resource");
        assert!(!html.contains("src="), "page loads an outside resource");
        assert!(!html.contains("@import"), "stylesheet imports");
        assert!(!html.contains("url("), "stylesheet fetches");
    }

    #[test]
    fn renders_fixture_without_outside_requests() {
        let store = fixture();
        let now = 1_792_400_000_000i64;
        for query in [
            "",
            "window=12w",
            "window=all&tool=codex",
            "tool=antigravity",
        ] {
            let html =
                render_dashboard(&store.conn, query, now, Some("2026-10-20T00:00:00Z".into()))
                    .unwrap();
            assert!(html.starts_with("<!doctype html>"));
            assert!(html.contains("</html>"));
            assert_offline(&html);
            assert!(html.contains("<section id=\"activity\""));
            assert!(html.contains("<section id=\"totals\""));
            assert!(html.contains("rates fetched 2026-10-20T00:00:00Z"));
        }
        let html = render_dashboard(&store.conn, "window=all", now, None).unwrap();
        assert_eq!(html.matches("<td class=\"c\"").count(), 7 * 24);
        assert!(html.contains("<tr class=\"total\"><th scope=\"row\">Total</th><td>3</td><td>600</td><td>6,000</td><td>0</td><td>220</td><td>$1.75*</td></tr>"));
        assert!(html.contains("tokens in 1 requests, est. $0.25\""));
        assert!(html.contains("query summary</code>"));
        // The filter narrows the ledger and recolours the ramp.
        let html = render_dashboard(&store.conn, "window=all&tool=codex", now, None).unwrap();
        assert!(html.contains("--ramp: var(--codex)"));
        assert!(!html.contains("<th scope=\"row\"><a class=\"tool\" data-tool=\"claude-code\""));
        assert!(html.contains("<th scope=\"row\"><a class=\"tool\" data-tool=\"codex\""));
        assert!(html.contains("codex rows of <code>query summary</code>"));
        // Outside the window: empty state with direction, no map.
        let html =
            render_dashboard(&store.conn, "window=4w", now + 100 * 86_400_000, None).unwrap();
        assert!(html.contains("No requests in this window. Widen the window"));
        assert!(!html.contains("id=\"map\""));
    }

    #[test]
    fn empty_store_points_at_scan() {
        let store = crate::store::Store::open_in_memory().unwrap();
        let html = render_dashboard(&store.conn, "", 1_792_400_000_000, None).unwrap();
        assert_offline(&html);
        assert!(html.contains("The store is empty."));
        assert!(html.contains("Run <code>usage-habit-report scan</code>"));
        let html = html::render_missing("/tmp/none/usage.db");
        assert_offline(&html);
        assert!(html.contains("Run <code>usage-habit-report scan</code>"));
    }

    #[test]
    fn labels_follow_the_zone() {
        let ts = 1_792_380_600_000i64; // 2026-10-19T03:30:00Z
        let ny = FixedOffset::west_opt(4 * 3600).unwrap();
        assert_eq!(clock_label(ts, &ny), "Sun 18 Oct 2026, 23:30");
        assert_eq!(date_label(ts, &ny), "18 Oct 2026");
        assert_eq!(tz_label(ts, &ny), "UTC-04:00");
        let india = FixedOffset::east_opt(5 * 3600 + 1800).unwrap();
        assert_eq!(tz_label(ts, &india), "UTC+05:30");
        assert_eq!(tz_label(ts, &FixedOffset::east_opt(0).unwrap()), "UTC");
    }
}
