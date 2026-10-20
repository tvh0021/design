//! Shared snapshot privacy matching. Errors never include private values.
use crate::serve::html;
use anyhow::{Result, bail};
use rusqlite::Connection;
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

// The snapshot templates emit lowercase style/script tags with no attributes.
pub fn body_text(document: &str) -> String {
    let mut body = document.to_owned();
    for tag in ["style", "script"] {
        let opening = format!("<{tag}>");
        let closing = format!("</{tag}>");
        while let Some(start) = body.find(&opening) {
            let end = body[start + opening.len()..]
                .find(&closing)
                .expect("inline asset has no closing tag")
                + start
                + opening.len()
                + closing.len();
            body.replace_range(start..end, "");
        }
    }
    body
}

fn is_word(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

pub fn contains_whole_word(body: &str, candidate: &str) -> bool {
    body.char_indices().any(|(start, _)| {
        if !body[start..].starts_with(candidate) {
            return false;
        }
        let end = start + candidate.len();
        (start == 0 || !is_word(body.as_bytes()[start - 1]))
            && (end == body.len() || !is_word(body.as_bytes()[end]))
    })
}

pub fn word_candidate_matches(body: &str, candidate: &str, vocabulary: &BTreeSet<String>) -> bool {
    candidate.chars().count() >= 3
        && !vocabulary.contains(candidate)
        && contains_whole_word(body, candidate)
}

/// Check every exported file, including inline assets, before publishing.
pub fn check(
    conn: &Connection,
    out: &Path,
    home: Option<&Path>,
    public_projects: &BTreeMap<String, String>,
    vocabulary: &BTreeSet<String>,
) -> Result<()> {
    let mut needles = BTreeSet::new();
    for column in [
        "cwd",
        "project",
        "session_id",
        "t3_thread_id",
        "display_title",
        "title",
        "t3_title",
    ] {
        let mut stmt = conn.prepare(&format!(
            "SELECT DISTINCT {column} FROM sessions WHERE {column} IS NOT NULL AND {column} != ''"
        ))?;
        for value in stmt.query_map([], |r| r.get::<_, String>(0))? {
            let value = value?;
            if matches!(column, "title" | "t3_title" | "display_title")
                && value.chars().count() < 12
            {
                continue;
            }
            needles.insert(html::esc(&value));
            needles.insert(value);
        }
    }
    if let Some(home) = home.filter(|p| !p.as_os_str().is_empty()) {
        let home = home.to_string_lossy().into_owned();
        needles.insert(html::esc(&home));
        needles.insert(home);
    }
    for entry in walkdir::WalkDir::new(out) {
        let entry = entry?;
        if !entry.file_type().is_file() {
            continue;
        }
        let body = std::fs::read_to_string(entry.path())?;
        if needles.iter().any(|candidate| body.contains(candidate)) {
            bail!("privacy guard failed: private store value found in export");
        }
        if without_public_urls(&body, public_projects).contains("https://") {
            bail!("privacy guard failed: unapproved external URL found in export");
        }
    }
    if audit(conn, out, vocabulary, public_projects)?
        .iter()
        .any(|count| count.matches > 0)
    {
        bail!("privacy guard failed: private store value found in export");
    }
    Ok(())
}

/// Counts from the original snapshot audit, including project-word checks.
/// Fixture vocabulary keeps common page words out of the optional word audit.
pub struct AuditCount {
    pub category: &'static str,
    pub candidates: usize,
    pub skipped_short: usize,
    pub vocabulary_words: usize,
    pub matches: usize,
    pub word_match: bool,
}

fn without_public_urls(body: &str, public_projects: &BTreeMap<String, String>) -> String {
    let mut text = body.to_owned();
    for url in public_projects.values() {
        text = text.replace(&html::esc(url), "").replace(url, "");
    }
    text
}

pub fn audit(
    conn: &Connection,
    out: &Path,
    vocabulary: &BTreeSet<String>,
    public_projects: &BTreeMap<String, String>,
) -> Result<Vec<AuditCount>> {
    // Exact categories retain whole-file raw and escaped substring checks.
    // Word categories use body text, a minimum length, and fixture vocabulary.
    let columns = [
        ("session_id", "session_id", false),
        ("t3_thread_id", "t3_thread_id", false),
        ("project", "project_path", false),
        ("project", "project_folder", true),
        ("project_name", "project_name", true),
        ("title", "title", true),
        ("display_title", "display_title", false),
        ("cwd", "cwd", false),
        ("t3_title", "t3_title", false),
    ];
    let mut categories = Vec::new();
    for (column, category, word_match) in columns {
        let mut stmt = conn.prepare(&format!(
            "SELECT DISTINCT {column} FROM sessions WHERE {column} IS NOT NULL AND {column} != ''"
        ))?;
        let values: Vec<String> = stmt
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        let mut needles = BTreeSet::new();
        for value in values {
            if matches!(column, "title" | "t3_title" | "display_title")
                && value.chars().count() < 12
            {
                continue;
            }
            let value = if column == "title" || column == "t3_title" {
                value.chars().take(20).collect::<String>()
            } else if category == "project_folder" {
                let Some(name) = Path::new(&value).file_name().and_then(|s| s.to_str()) else {
                    continue;
                };
                name.to_owned()
            } else {
                value
            };
            if word_match
                && public_projects
                    .values()
                    .any(|url| crate::public_projects::name(url) == value)
            {
                continue;
            }
            needles.insert(html::esc(&value));
            needles.insert(value);
        }
        categories.push((category, needles, word_match));
    }
    let mut matches = vec![0usize; categories.len()];
    for entry in walkdir::WalkDir::new(out) {
        let entry = entry?;
        if !entry.file_type().is_file() {
            continue;
        }
        let body = std::fs::read_to_string(entry.path())?;
        let text = body_text(&without_public_urls(&body, public_projects));
        for (i, (_, needles, word_match)) in categories.iter().enumerate() {
            matches[i] += needles
                .iter()
                .filter(|candidate| {
                    if *word_match {
                        word_candidate_matches(&text, candidate, vocabulary)
                    } else {
                        body.contains(candidate.as_str())
                    }
                })
                .count();
        }
    }
    Ok(categories
        .into_iter()
        .zip(matches)
        .map(|((category, needles, word_match), matches)| AuditCount {
            category,
            candidates: needles.len(),
            skipped_short: needles
                .iter()
                .filter(|s| word_match && s.chars().count() < 3)
                .count(),
            vocabulary_words: needles
                .iter()
                .filter(|s| word_match && s.chars().count() >= 3 && vocabulary.contains(*s))
                .count(),
            matches,
            word_match,
        })
        .collect())
}
