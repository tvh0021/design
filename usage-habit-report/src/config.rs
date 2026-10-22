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
    /// Plan behind each tool: `codex = 20 "ChatGPT Plus"`. Tools not listed
    /// use `value::default_subscription`.
    pub subscriptions: BTreeMap<String, crate::value::Subscription>,
}

/// `<tool> = <monthly USD> ["plan name"] [# comment]`.
fn subscription(line: &str) -> Option<(String, crate::value::Subscription)> {
    let (tool, rest) = line.split_once('=')?;
    let tool = tool.trim();
    if tool.is_empty() || !tool.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
        return None;
    }
    let rest = rest.split('#').next().unwrap_or("").trim();
    let (price, tail) = match rest.find(|c: char| c.is_whitespace()) {
        Some(i) => rest.split_at(i),
        None => (rest, ""),
    };
    let monthly_usd = match price {
        "none" => None,
        p => Some(
            p.parse::<f64>()
                .ok()
                .filter(|p| p.is_finite() && *p >= 0.0)?,
        ),
    };
    let name = if tail.trim().is_empty() {
        crate::value::default_subscription(tool).name
    } else {
        let (name, after) = crate::public_projects::quoted(tail)?;
        if !after.trim().is_empty() {
            return None;
        }
        name
    };
    Some((
        tool.to_owned(),
        crate::value::Subscription { name, monthly_usd },
    ))
}

pub fn load() -> Result<Config> {
    let text = match std::fs::read_to_string(crate::paths::config_file()?) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Config::default()),
        Err(e) => return Err(e).context("reading usage-habit-report config"),
    };
    parse(&text)
}

pub fn parse(text: &str) -> Result<Config> {
    let mut out = Config::default();
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
                    "invalid subscription on line {}: expected `tool = 20 \"plan\"`",
                    index + 1
                )
            })?;
            out.subscriptions.insert(tool, plan);
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
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn subscriptions_take_a_price_and_an_optional_name() {
        let c = parse(
            "[subscriptions]\ncodex = 200 \"ChatGPT Pro\" # upgraded\nclaude-code = 150\nantigravity = none 'Free tier'\n",
        )
        .unwrap();
        assert_eq!(c.subscriptions["codex"].name, "ChatGPT Pro");
        assert_eq!(c.subscriptions["codex"].monthly_usd, Some(200.0));
        assert_eq!(
            c.subscriptions["claude-code"].name,
            "Claude Team, premium seat"
        );
        assert_eq!(c.subscriptions["claude-code"].monthly_usd, Some(150.0));
        assert_eq!(c.subscriptions["antigravity"].monthly_usd, None);
        assert_eq!(c.subscriptions["antigravity"].name, "Free tier");
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
