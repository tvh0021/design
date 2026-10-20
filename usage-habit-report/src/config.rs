//! The application's small, quoted-scalar configuration format.
use anyhow::{Context, Result, bail};
use std::{collections::BTreeMap, path::PathBuf};

#[derive(Default, Debug)]
pub struct Config {
    pub sources: BTreeMap<String, PathBuf>,
    pub public_projects: BTreeMap<String, String>,
    pub publish_dir: Option<PathBuf>,
    pub publish_push: Option<bool>,
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
