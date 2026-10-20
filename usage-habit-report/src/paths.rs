//! Default locations. XDG variables win when set; otherwise the Linux-style
//! defaults are used on every platform (including macOS).

use anyhow::{Context, Result};
use std::path::PathBuf;

pub const APP: &str = "usage-habit-report";

fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .context("HOME is not set")
}

fn xdg(var: &str, fallback: &[&str]) -> Result<PathBuf> {
    match std::env::var_os(var) {
        Some(v) if !v.is_empty() => Ok(PathBuf::from(v)),
        _ => Ok(fallback.iter().fold(home()?, |p, s| p.join(s))),
    }
}

/// `~/.local/share/usage-habit-report`
pub fn data_dir() -> Result<PathBuf> {
    Ok(xdg("XDG_DATA_HOME", &[".local", "share"])?.join(APP))
}

/// `~/.config/usage-habit-report`
pub fn config_dir() -> Result<PathBuf> {
    Ok(xdg("XDG_CONFIG_HOME", &[".config"])?.join(APP))
}

/// `~/.local/share/usage-habit-report/usage.db`
pub fn default_db() -> Result<PathBuf> {
    Ok(data_dir()?.join("usage.db"))
}

/// `~/.claude/projects`
pub fn claude_projects() -> Result<PathBuf> {
    Ok(home()?.join(".claude").join("projects"))
}
