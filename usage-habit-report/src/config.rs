//! The application's small, quoted-scalar configuration format.
use anyhow::{Context, Result, bail};
use std::{collections::BTreeMap, path::PathBuf};

#[derive(Default, Debug)]
pub struct Config {
    pub sources: BTreeMap<String, PathBuf>,
    pub project_aliases: BTreeMap<String, String>,
    pub public_projects: BTreeMap<String, String>,
    pub publish_dir: Option<PathBuf>,
    pub publish_push: Option<bool>,
    /// Plans behind each tool, one per `[subscriptions]` line, in file
    /// order: `codex = 20 "ChatGPT Plus"`. A tool may have several lines
    /// (two seats, or a plan change). Tools not listed use
    /// `value::default_subscription`.
    pub subscriptions: BTreeMap<String, Vec<crate::value::Plan>>,
}

/// `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`, local time. A bare date is the start
/// of that day, or with `end_of_day` the start of the next one.
fn local_time(text: &str, end_of_day: bool) -> Option<chrono::NaiveDateTime> {
    if let Ok(t) = chrono::NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M") {
        return Some(t);
    }
    let day = chrono::NaiveDate::parse_from_str(text, "%Y-%m-%d").ok()?;
    let day = if end_of_day { day.succ_opt()? } else { day };
    day.and_hms_opt(0, 0, 0)
}

fn price(text: &str) -> Option<f64> {
    text.parse::<f64>()
        .ok()
        .filter(|p| p.is_finite() && *p >= 0.0)
}

/// `<tool> = <list USD|none> ["plan name"] [paid=<USD>] [account=<12 hex>]
/// [label="text"] [from=<date>] [until=<date>] [remote] [# comment]`.
fn subscription(line: &str) -> Option<(String, crate::value::Plan)> {
    let (tool, rest) = line.split_once('=')?;
    let tool = tool.trim();
    if tool.is_empty() || !tool.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
        return None;
    }
    let rest = rest.trim_start();
    let end = rest
        .find(|c: char| c.is_whitespace() || c == '#')
        .unwrap_or(rest.len());
    let (list, mut tail) = rest.split_at(end);
    let monthly_usd = match list {
        "none" => None,
        p => Some(price(p)?),
    };
    let mut plan = crate::value::Plan {
        name: crate::value::default_subscription(tool).name,
        monthly_usd,
        paid_monthly_usd: monthly_usd,
        account: None,
        label: None,
        from: None,
        until: None,
        remote: false,
    };
    tail = tail.trim_start();
    if tail.starts_with(['"', '\'']) {
        let (name, after) = crate::public_projects::quoted(tail)?;
        plan.name = name;
        tail = after;
    }
    let mut seen = std::collections::BTreeSet::new();
    loop {
        tail = tail.trim_start();
        if tail.is_empty() || tail.starts_with('#') {
            break;
        }
        let key_end = tail
            .find(|c: char| c.is_whitespace() || c == '=')
            .unwrap_or(tail.len());
        let (key, after) = tail.split_at(key_end);
        if !seen.insert(key.to_owned()) {
            return None;
        }
        if key == "remote" && !after.starts_with('=') {
            plan.remote = true;
            tail = after;
            continue;
        }
        let after = after.strip_prefix('=')?;
        let (value, after) = if key == "label" {
            crate::public_projects::quoted(after)?
        } else {
            let end = after
                .find(|c: char| c.is_whitespace())
                .unwrap_or(after.len());
            (after[..end].to_owned(), &after[end..])
        };
        if !after.is_empty() && !after.starts_with(char::is_whitespace) {
            return None;
        }
        tail = after;
        match key {
            "paid" => plan.paid_monthly_usd = Some(price(&value)?),
            "account" => {
                if value.len() != 12 || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
                    return None;
                }
                plan.account = Some(value.to_ascii_lowercase());
            }
            "label" => plan.label = Some(value).filter(|l| !l.trim().is_empty()),
            "from" => plan.from = Some(local_time(&value, false)?),
            "until" => plan.until = Some(local_time(&value, true)?),
            _ => return None,
        }
    }
    if let (Some(from), Some(until)) = (plan.from, plan.until)
        && from >= until
    {
        return None;
    }
    Some((tool.to_owned(), plan))
}

pub fn load() -> Result<Config> {
    // Unit tests never read the user's own config.
    if cfg!(test) && std::env::var_os("USAGE_HABIT_REPORT_CONFIG").is_none() {
        return Ok(Config::default());
    }
    let text = match std::fs::read_to_string(crate::paths::config_file()?) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Config::default()),
        Err(e) => return Err(e).context("reading usage-habit-report config"),
    };
    parse(&text)
}

pub fn parse(text: &str) -> Result<Config> {
    let mut out = Config::default();
    // tool -> the config line of each of its plans, in order.
    let mut plan_lines: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    let mut section = "";
    for (index, line) in text.lines().map(str::trim).enumerate() {
        if line.starts_with('[') {
            section = line.split('#').next().unwrap().trim();
            continue;
        }
        if section == "[project_aliases]" {
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let (source, tail) =
                crate::public_projects::quoted(line).context("invalid project alias source")?;
            let (target, tail) = crate::public_projects::quoted(
                tail.trim_start()
                    .strip_prefix('=')
                    .context("invalid project alias")?,
            )
            .context("invalid project alias target")?;
            if (!tail.trim().is_empty() && !tail.trim_start().starts_with('#'))
                || !std::path::Path::new(&source).is_absolute()
                || !std::path::Path::new(&target).is_absolute()
                || source == "/"
                || target == "/"
            {
                bail!("project aliases require absolute non-root paths");
            }
            out.project_aliases.insert(source, target);
            continue;
        }
        if section == "[subscriptions]" {
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let (tool, plan) = subscription(line).with_context(|| {
                format!(
                    "invalid subscription on line {}: expected `tool = 20 \"plan\" [paid=0] [account=<12 hex>] [label=\"name\"] [from=YYYY-MM-DD] [until=YYYY-MM-DD] [remote]`",
                    index + 1
                )
            })?;
            plan_lines.entry(tool.clone()).or_default().push(index + 1);
            out.subscriptions.entry(tool).or_default().push(plan);
            continue;
        }
        if section == "[public_projects]" {
            if !line.is_empty() && !line.starts_with('#') {
                if let Some((path, url)) = crate::public_projects::setting(line) {
                    out.public_projects.insert(path, url);
                } else {
                    eprintln!(
                        "warning: ignoring malformed public_projects setting on line {}",
                        index + 1
                    );
                }
            }
            continue;
        }
        if !matches!(section, "[sources]" | "[publish]") || line.is_empty() || line.starts_with('#')
        {
            continue;
        }
        let (key, value) = line.split_once('=').context("invalid config setting")?;
        let (key, value) = (key.trim(), value.trim());
        if section == "[publish]" && key == "push" {
            out.publish_push = Some(match value.split('#').next().unwrap().trim() {
                "true" => true,
                "false" => false,
                _ => bail!("publish push must be true or false"),
            });
            continue;
        }
        if !(section == "[publish]" && key == "dir"
            || section == "[sources]"
                && matches!(
                    key,
                    "codex_state_dir" | "codex_session_index" | "antigravity_summaries"
                ))
        {
            bail!("unknown config setting");
        }
        let path = if let Some(value) = value.strip_prefix('\'') {
            let (path, tail) = value.split_once('\'').context("unterminated config path")?;
            if !tail.trim().is_empty() && !tail.trim().starts_with('#') {
                bail!("invalid config path");
            }
            path.to_owned()
        } else {
            let mut stream = serde_json::Deserializer::from_str(value).into_iter::<String>();
            let path = stream.next().context("missing config path")??;
            let tail = value[stream.byte_offset()..].trim();
            if !tail.is_empty() && !tail.starts_with('#') {
                bail!("invalid config path");
            }
            path
        };
        let path = if let Some(rest) = path.strip_prefix("~/") {
            PathBuf::from(std::env::var_os("HOME").context("HOME is not set")?).join(rest)
        } else {
            path.into()
        };
        if section == "[publish]" {
            out.publish_dir = Some(path);
        } else {
            out.sources.insert(key.to_owned(), path);
        }
    }
    for (tool, plans) in &out.subscriptions {
        let lines = &plan_lines[tool];
        for (i, a) in plans.iter().enumerate() {
            for (j, b) in plans.iter().enumerate().skip(i + 1) {
                // `until` is exclusive, so a plan may start as another ends.
                let starts_before = |p: &crate::value::Plan, q: &crate::value::Plan| {
                    p.from.zip(q.until).is_none_or(|(f, u)| f < u)
                };
                if a.account.is_some()
                    && a.account == b.account
                    && starts_before(a, b)
                    && starts_before(b, a)
                {
                    bail!(
                        "subscription lines {} ({tool} \"{}\") and {} ({tool} \"{}\") overlap for account {}; end one where the other starts",
                        lines[i],
                        a.name,
                        lines[j],
                        b.name,
                        a.account.as_deref().unwrap_or_default(),
                    );
                }
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subscriptions_take_a_price_and_an_optional_name() {
        let c = parse(
            "[subscriptions]\ncodex = 200 \"ChatGPT Pro\" # upgraded\nclaude-code = 125\nantigravity = none 'Free tier'\n",
        )
        .unwrap();
        let one = |tool: &str| {
            assert_eq!(c.subscriptions[tool].len(), 1);
            &c.subscriptions[tool][0]
        };
        assert_eq!(one("codex").name, "ChatGPT Pro");
        assert_eq!(one("codex").monthly_usd, Some(200.0));
        assert_eq!(one("codex").paid_monthly_usd, Some(200.0));
        assert_eq!(one("claude-code").name, "Claude Team, premium seat");
        assert_eq!(one("claude-code").monthly_usd, Some(125.0));
        assert_eq!(one("antigravity").monthly_usd, None);
        assert_eq!(one("antigravity").name, "Free tier");
        for line in [
            "codex = twenty",
            "codex = -1",
            "codex = 20 unquoted",
            "= 20",
            "codex 20",
        ] {
            assert!(
                parse(&format!("[subscriptions]\n{line}")).is_err(),
                "{line}"
            );
        }
    }

    #[test]
    fn repeated_tool_lines_add_seats_with_accounts_dates_and_paid_prices() {
        let c = parse(concat!(
            "[subscriptions]\n",
            "codex = 20 \"ChatGPT Plus\" account=AAAABBBBCCCC label=\"Home # 1\"\n",
            "codex = 20 \"ChatGPT Plus\" paid=0 account=444455556666 label='Lab' from=2026-11-16T21:40 # second seat\n",
            "antigravity = 200 'Google AI Ultra' paid=20 remote until=2026-12-31\n",
        ))
        .unwrap();
        let codex = &c.subscriptions["codex"];
        assert_eq!(codex.len(), 2);
        assert_eq!(codex[0].account.as_deref(), Some("aaaabbbbcccc"));
        assert_eq!(codex[0].label.as_deref(), Some("Home # 1"));
        assert_eq!(codex[0].paid_monthly_usd, Some(20.0));
        assert_eq!(
            (codex[0].from, codex[0].until, codex[0].remote),
            (None, None, false)
        );
        assert_eq!(codex[1].paid_monthly_usd, Some(0.0));
        assert_eq!(codex[1].label.as_deref(), Some("Lab"));
        assert_eq!(codex[1].from.unwrap().to_string(), "2026-11-16 21:40:00");
        let ultra = &c.subscriptions["antigravity"][0];
        assert!(ultra.remote);
        assert_eq!(ultra.paid_monthly_usd, Some(20.0));
        // A bare `until` date includes that whole day.
        assert_eq!(ultra.until.unwrap().to_string(), "2027-01-01 00:00:00");
        for line in [
            "codex = 20 account=1234567890a",
            "codex = 20 account=1234567890ag",
            "codex = 20 paid=-5",
            "codex = 20 paid=none",
            "codex = 20 seats=2",
            "codex = 20 paid=0 paid=1",
            "codex = 20 remote remote",
            "codex = 20 remote=yes",
            "codex = 20 label=unquoted",
            "codex = 20 from=2026-11-31",
            "codex = 20 from=2026-11-20 until=2026-11-19",
            "codex = 20 from=2026-11-20T10:00 until=2026-11-20T10:00",
            "codex = 20 \"ChatGPT Plus\" stray",
        ] {
            assert!(
                parse(&format!("[subscriptions]\n{line}")).is_err(),
                "{line}"
            );
        }
        // A one-day plan is valid: `until` covers the day it names.
        assert!(parse("[subscriptions]\ncodex = 20 from=2026-11-20 until=2026-11-20").is_ok());
    }

    #[test]
    fn one_accounts_plans_may_touch_but_not_overlap() {
        let two = |a: &str, b: &str| {
            parse(&format!(
                "[subscriptions]\ncodex = 20 account=0123456789ab {a}\ncodex = 200 \"ChatGPT Pro\" account=0123456789ab {b}\n"
            ))
        };
        assert!(two("until=2026-11-10T20:35", "from=2026-11-10T20:35").is_ok());
        assert!(two("until=2026-11-18", "from=2026-11-19").is_ok());
        let err = two("until=2026-11-19", "from=2026-11-19").unwrap_err();
        assert!(
            err.to_string()
                .contains("lines 2 (codex \"ChatGPT Plus\") and 3"),
            "{err}"
        );
        assert!(two("", "from=2026-11-19").is_err());
        // Two seats on different accounts run side by side.
        assert!(parse("[subscriptions]\ncodex = 20 account=0123456789ab\ncodex = 20 account=ba9876543210\n").is_ok());
    }

    #[test]
    fn project_aliases_accept_quoted_paths_and_reject_unsafe_or_malformed_rules() {
        let c = parse("[project_aliases]\n'/work/arena' = \"/work/arena\" # umbrella\n").unwrap();
        assert_eq!(c.project_aliases["/work/arena"], "/work/arena");
        for line in [
            "'relative' = '/work'",
            "'/work' = '/'",
            "'/' = '/work'",
            "'/work' = '/other' junk",
        ] {
            assert!(parse(&format!("[project_aliases]\n{line}")).is_err());
        }
    }
}
