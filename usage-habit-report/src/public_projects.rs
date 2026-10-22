//! Explicit public-project opt-ins from the application's config file.
use anyhow::Result;
use std::collections::BTreeMap;

pub fn name(url: &str) -> &str {
    url.trim_end_matches('/').rsplit('/').next().unwrap_or("")
}

pub(crate) fn quoted(input: &str) -> Option<(String, &str)> {
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

pub(crate) fn setting(line: &str) -> Option<(String, String)> {
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

pub fn load() -> Result<BTreeMap<String, String>> {
    Ok(crate::config::load()?.public_projects)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quoted_paths_urls_comments_and_sections() {
        let projects = crate::config::parse(
            r#"
[sources]
codex_state_dir = '/home/demo/.codex'
[public_projects] # repositories
"/home/demo/a=b" = "https://github.com/demo/alpha" # comment
'/home/demo/b' = 'https://github.com/demo/alpha/'
[publish]
push = false
"#,
        )
        .unwrap()
        .public_projects;
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
            assert!(
                crate::config::parse(&format!("[public_projects]\n{line}"))
                    .unwrap()
                    .public_projects
                    .is_empty()
            );
        }
    }
}
