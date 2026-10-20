//! Guarded replacement and scoped git publication of public snapshots.
use anyhow::{Context, Result, bail};
use std::{path::Path, process::Command};

pub const MESSAGE: &str = "Refresh the public usage snapshot";

fn git(dir: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("git").arg("-C").arg(dir).args(args).output()?;
    if !output.status.success() {
        // Git diagnostics can contain private paths or remote credentials.
        bail!("git {} failed", args[0]);
    }
    Ok(String::from_utf8(output.stdout)?.trim().to_owned())
}

pub fn run(db: &Path, out: &Path, push: bool, now_ms: i64) -> Result<String> {
    if out.join(".git").exists() || out.is_symlink() {
        bail!("publish directory must be a regular directory below the repository root");
    }
    let parent = out
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    std::fs::create_dir_all(parent)?;
    let stage = tempfile::Builder::new()
        .prefix(".usage-publish-")
        .tempdir_in(parent)?;
    let pages = stage.path().join("pages");
    let files = crate::serve::snapshot::write(db, &pages, None, now_ms)?;
    let conn = crate::serve::open_read_only(db)?.context("missing usage store")?;
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from);
    for entry in std::fs::read_dir(&pages)? {
        let entry = entry?;
        let old = out.join(entry.file_name());
        if let Ok(previous) = std::fs::read_to_string(old) {
            let next = std::fs::read_to_string(entry.path())?;
            if without_timing(&previous) == without_timing(&next) {
                std::fs::write(entry.path(), previous)?;
            }
        }
    }
    let public_projects = crate::config::load()?.public_projects;
    crate::privacy::check(
        &conn,
        &pages,
        home.as_deref(),
        &public_projects,
        &Default::default(),
    )?;
    // Keep the previous tree until the guarded replacement succeeds.
    let backup = stage.path().join("previous");
    if out.exists() {
        std::fs::rename(out, &backup)?;
    }
    if let Err(e) = std::fs::rename(&pages, out) {
        if backup.exists() {
            std::fs::rename(&backup, out)?;
        }
        return Err(e.into());
    }
    let out = std::fs::canonicalize(out)?;
    let discovery = Command::new("git")
        .arg("-C")
        .arg(&out)
        .args(["rev-parse", "--show-toplevel"])
        .env("LC_ALL", "C")
        .output()?;
    if !discovery.status.success() {
        if String::from_utf8_lossy(&discovery.stderr).contains("not a git repository") {
            return Ok(format!(
                "publish: {files} files written; no commit; not pushed (not a git repository)"
            ));
        }
        bail!("git rev-parse failed");
    }
    let root = std::path::PathBuf::from(String::from_utf8(discovery.stdout)?.trim());
    let relative = out
        .strip_prefix(&root)?
        .to_str()
        .context("non-UTF-8 publish directory")?;
    if relative.is_empty() {
        bail!("publish directory must be below the repository root");
    }
    // Refuse to include someone else's staged work in our commit.
    if !git(&root, &["diff", "--cached", "--name-only"])?.is_empty() {
        bail!("git index already has staged changes");
    }
    git(&root, &["add", "--", relative])?;
    let changed = !git(&root, &["diff", "--cached", "--name-only", "--", relative])?.is_empty();
    if changed {
        git(&root, &["commit", "-m", MESSAGE])?;
    }
    let reason = if !push {
        "not pushed (disabled)".to_owned()
    } else {
        let log = git(&root, &["log", "@{u}..HEAD", "--format=%s"])?;
        let others = log.lines().filter(|line| *line != MESSAGE).count();
        if others > 0 {
            format!("not pushed ({others} other unpushed commits)")
        } else {
            git(&root, &["push"])?;
            "pushed".to_owned()
        }
    };
    Ok(format!(
        "publish: {files} files written; {}; {reason}",
        if changed { "committed" } else { "no commit" }
    ))
}

fn without_timing(body: &str) -> String {
    // Retain the previous generation clock when every exported figure and date
    // bucket is unchanged. The clock also appears in window labels.
    let body = if let Some((_, note)) = body.split_once("De-identified export. Generated ") {
        if let Some((clock, _)) = note.split_once(". Window:") {
            body.replace(clock, "GENERATION_CLOCK")
        } else {
            body.to_owned()
        }
    } else {
        body.to_owned()
    };
    let Some((before, after)) = body.split_once("Data loaded in ") else {
        return body.to_owned();
    };
    let Some((_, tail)) = after.split_once(" ms.") else {
        return body.to_owned();
    };
    format!("{before}Data loaded in ms.{tail}")
}
