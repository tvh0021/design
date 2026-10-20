//! Explicit public-project opt-ins from the application's config file.
use anyhow::{Context, Result};
use std::collections::BTreeMap;

pub fn name(url: &str) -> &str {
    url.trim_end_matches('/').rsplit('/').next().unwrap_or("")
}

fn quoted(input: &str) -> Option<(String, &str)> {
    let input = input.trim_start();
    if let Some(rest) = input.strip_prefix('\'') {
        let (value, tail) = rest.split_once('\'')?;
        Some((value.to_owned(), tail))
    } else {
        let mut stream = serde_json::Deserializer::from_str(input).into_iter::<String>();
        let value = stream.next()?.ok()?;
        Some((value, &input[stream.byte_offset()..]))
    }
}

fn setting(line: &str) -> Option<(String, String)> {
    let (path, tail) = quoted(line)?;
    let (url, tail) = quoted(tail.trim_start().strip_prefix('=')?)?;
    if !tail.trim().is_empty() && !tail.trim_start().starts_with('#') {
        return None;
    }
    // Only repository links, never executable schemes or non-GitHub hosts.
    let repo = url
        .strip_prefix("https://github.com/")?
        .trim_end_matches('/');
    let parts: Vec<_> = repo.split('/').collect();
    if path.is_empty()
        || parts.len() != 2
        || parts.iter().any(|p| {
            p.is_empty()
                || *p == "."
                || *p == ".."
                || !p
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
        })
    {
        return None;
    }
    Some((path, url))
}

pub fn parse(text: &str) -> BTreeMap<String, String> {
    let mut projects = BTreeMap::new();
    let mut active = false;
    for (index, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.starts_with('[') {
            active = line.split('#').next().unwrap_or("").trim() == "[public_projects]";
        } else if active && !line.is_empty() && !line.starts_with('#') {
            if let Some((path, url)) = setting(line) {
                projects.insert(path, url);
            } else {
                eprintln!(
                    "warning: ignoring malformed public_projects setting on line {}",
                    index + 1
                );
            }
        }
    }
    projects
}

pub fn load() -> Result<BTreeMap<String, String>> {
    match std::fs::read_to_string(crate::paths::config_file()?) {
        Ok(text) => Ok(parse(&text)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(BTreeMap::new()),
        Err(e) => Err(e).context("reading usage-habit-report public projects config"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quoted_paths_urls_comments_and_sections() {
        let projects = parse(
            r#"
[sources]
codex_state_dir = '/home/demo/.codex'
[public_projects] # repositories
"/home/demo/a=b" = "https://github.com/demo/alpha" # comment
'/home/demo/b' = 'https://github.com/demo/alpha/'
[publish]
invalid = 'ignored'
"#,
        );
        assert_eq!(projects.len(), 2);
        assert_eq!(name(&projects["/home/demo/a=b"]), "alpha");
        assert_eq!(name(&projects["/home/demo/b"]), "alpha");
    }

    #[test]
    fn malformed_settings_and_unsafe_links_are_ignored() {
        for line in [
            "unquoted = 'https://github.com/demo/repo'",
            "'/home/demo/a' = unquoted",
            "'/home/demo/a' 'https://github.com/demo/repo'",
            "'/home/demo/a' = 'https://github.com/demo/repo' trailing",
            "'/home/demo/a' = 'javascript:alert(1)'",
            "'/home/demo/a' = 'https://github.com.evil/demo/repo'",
            "'/home/demo/a' = 'https://github.com/demo/repo/issues'",
            "'/home/demo/a' = 'https://github.com/demo/..'",
        ] {
            assert!(parse(&format!("[public_projects]\n{line}")).is_empty());
        }
    }
}
