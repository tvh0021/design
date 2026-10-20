//! Optional harness headlines, refreshed independently of transcript cursors.
use anyhow::{Context, Result, bail};
use rusqlite::{Connection, OpenFlags, params};
use std::{
    collections::HashMap,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
};

#[derive(Debug)]
pub struct Sources {
    pub codex_state_dir: PathBuf,
    pub codex_session_index: PathBuf,
    pub antigravity_summaries: PathBuf,
}
impl Sources {
    pub fn load(codex_home: &Path) -> Result<Self> {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_default();
        let mut out = Self {
            codex_state_dir: codex_home.to_owned(),
            codex_session_index: codex_home.join("session_index.jsonl"),
            antigravity_summaries: home.join(".gemini/antigravity/conversation_summaries.db"),
        };
        let config = crate::paths::config_dir()?.join("config.toml");
        let text = match std::fs::read_to_string(&config) {
            Ok(text) => text,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
            Err(e) => return Err(e).context("reading usage-habit-report config"),
        };
        // Only quoted scalar source paths are supported. Never inspect a
        // harness's own config file, which may contain credentials.
        let mut sources = false;
        for line in text.lines().map(str::trim) {
            if line.starts_with('[') {
                sources = line == "[sources]";
                continue;
            }
            if !sources || line.is_empty() || line.starts_with('#') {
                continue;
            }
            let Some((key, value)) = line.split_once('=') else {
                bail!("invalid sources setting");
            };
            let slot = match key.trim() {
                "codex_state_dir" => &mut out.codex_state_dir,
                "codex_session_index" => &mut out.codex_session_index,
                "antigravity_summaries" => &mut out.antigravity_summaries,
                _ => bail!("unknown sources setting"),
            };
            let value = value.trim();
            let path: String = if let Some(value) = value.strip_prefix('\'') {
                let (path, tail) = value.split_once('\'').context("unterminated source path")?;
                if !tail.trim().is_empty() && !tail.trim().starts_with('#') {
                    bail!("invalid source path");
                }
                path.to_owned()
            } else {
                let mut stream = serde_json::Deserializer::from_str(value).into_iter::<String>();
                let path = stream.next().context("missing source path")??;
                let tail = value[stream.byte_offset()..].trim();
                if !tail.is_empty() && !tail.starts_with('#') {
                    bail!("invalid source path");
                }
                path
            };
            *slot = if let Some(path) = path.strip_prefix("~/") {
                home.join(path)
            } else {
                path.into()
            };
        }
        Ok(out)
    }
}
type Titles = HashMap<(String, String), (String, &'static str)>;
pub fn normalize(title: &str) -> Option<String> {
    let title: String = title
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(120)
        .collect();
    (!title.is_empty()).then_some(title)
}
fn open(path: &Path) -> Result<Connection> {
    Ok(Connection::open_with_flags(
        crate::antigravity::sqlite_uri(path, "mode=ro&immutable=1"),
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?)
}
fn codex_db(dir: &Path) -> Result<Titles> {
    let path = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .filter_map(|e| {
            let name = e.file_name();
            let n = name
                .to_str()?
                .strip_prefix("state_")?
                .strip_suffix(".sqlite")?
                .parse::<u64>()
                .ok()?;
            Some((n, e.path()))
        })
        .max_by_key(|(n, _)| *n)
        .context("no Codex title database")?
        .1;
    let c = open(&path)?;
    let mut stmt = c.prepare("SELECT id, name, title FROM threads")?;
    let mut out = Titles::new();
    for row in stmt.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<String>>(1)?,
            r.get::<_, Option<String>>(2)?,
        ))
    })? {
        let (id, name, title) = row?;
        if let Some((title, source)) = name
            .as_deref()
            .and_then(normalize)
            .map(|s| (s, "codex-name"))
            .or_else(|| {
                title
                    .as_deref()
                    .and_then(normalize)
                    .map(|s| (s, "codex-title"))
            })
        {
            out.insert(("codex".into(), id), (title, source));
        }
    }
    Ok(out)
}
fn index(path: &Path) -> Result<Titles> {
    let mut out = Titles::new();
    for line in BufReader::new(std::fs::File::open(path)?).lines() {
        let line = line?;
        let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) else {
            continue;
        };
        if let (Some(id), Some(title)) = (
            v["id"].as_str(),
            v["thread_name"].as_str().and_then(normalize),
        ) {
            out.insert(("codex".into(), id.into()), (title, "codex-name"));
        }
    }
    Ok(out)
}
fn antigravity(path: &Path) -> Result<Titles> {
    let c = open(path)?;
    let mut stmt = c.prepare("SELECT conversation_id, title FROM conversation_summaries")?;
    let mut out = Titles::new();
    for row in stmt.query_map([], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
    })? {
        let (id, title) = row?;
        if let Some(title) = title.as_deref().and_then(normalize) {
            out.insert(("antigravity".into(), id), (title, "antigravity-summary"));
        }
    }
    Ok(out)
}
/// Return an aggregate skipped-source count, without exposing paths or titles.
pub fn refresh(conn: &mut Connection, sources: &Sources) -> Result<usize> {
    let mut skipped = 0;
    let mut titles = match codex_db(&sources.codex_state_dir) {
        Ok(t) => t,
        Err(_) => {
            skipped += 1;
            Titles::new()
        }
    };
    match index(&sources.codex_session_index) {
        Ok(t) => {
            for (key, value) in t {
                if titles.get(&key).is_none_or(|v| v.1 != "codex-name") {
                    titles.insert(key, value);
                }
            }
        }
        Err(_) => skipped += 1,
    }
    match antigravity(&sources.antigravity_summaries) {
        Ok(t) => titles.extend(t),
        Err(_) => skipped += 1,
    }
    let tx = conn.transaction()?;
    let rows = tx
        .prepare("SELECT tool, session_id, title, t3_title FROM sessions")?
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<String>>(3)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    for (tool, id, raw, t3) in rows {
        let chosen = titles
            .get(&(tool.clone(), id.clone()))
            .cloned()
            .or_else(|| t3.as_deref().and_then(normalize).map(|s| (s, "t3")));
        let (title, source) = match chosen {
            Some((title, source)) => (Some(title), source),
            None => (raw.as_deref().and_then(normalize), "first-line"),
        };
        tx.execute("UPDATE sessions SET display_title = ?3, title_source = ?4 WHERE tool = ?1 AND session_id = ?2 AND (display_title IS NOT ?3 OR title_source IS NOT ?4)", params![tool, id, title, source])?;
    }
    tx.commit()?;
    Ok(skipped)
}
