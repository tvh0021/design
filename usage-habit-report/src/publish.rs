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
        let others = other_commits(&root, "@{u}..HEAD", relative)?;
        if others > 0 {
            format!("not pushed ({others} other unpushed commits)")
        } else {
            git(&root, &["push"])?;
            format!("pushed{}", update_default_branch(&root, relative)?)
        }
    };
    Ok(format!(
        "publish: {files} files written; {}; {reason}",
        if changed { "committed" } else { "no commit" }
    ))
}

/// Counts the commits in `range` that are not snapshot commits. A snapshot
/// commit has the snapshot message, one parent, and no changes outside the
/// snapshot directory, so an amended or reworded commit cannot carry other
/// files out. Each commit is checked, because a later commit can revert an
/// earlier one and hide it from the combined diff.
fn other_commits(root: &Path, range: &str, relative: &str) -> Result<usize> {
    let log = git(
        root,
        &[
            "log",
            "--no-show-signature",
            "--format=%H%x09%P%x09%s",
            range,
        ],
    )?;
    let outside = format!(":(top,literal,exclude){relative}");
    let mut others = 0;
    for line in log.lines() {
        let mut fields = line.splitn(3, '\t');
        let commit = fields.next().unwrap_or_default();
        let parents = fields.next().unwrap_or_default();
        let subject = fields.next().unwrap_or_default();
        // Merges and root commits have no single diff to check.
        let snapshot = subject == MESSAGE
            && parents.split_whitespace().count() == 1
            && git(
                root,
                &[
                    "diff-tree",
                    "-r",
                    "-z",
                    "--no-commit-id",
                    "--no-renames",
                    "--name-only",
                    commit,
                    "--",
                    &outside,
                ],
            )?
            .is_empty();
        others += usize::from(!snapshot);
    }
    Ok(others)
}

/// GitHub Pages deploys the remote's default branch, so a snapshot pushed to a
/// project branch also fast-forwards the default branch. It moves only when
/// every commit it lacks is a snapshot commit: unmerged code never ships.
fn update_default_branch(root: &Path, relative: &str) -> Result<String> {
    let branch = git(root, &["symbolic-ref", "--short", "HEAD"])?;
    let remote = git(root, &["config", &format!("branch.{branch}.remote")])?;
    let upstream = git(
        root,
        &["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
    )?;
    let default = git(
        root,
        &[
            "symbolic-ref",
            "--short",
            &format!("refs/remotes/{remote}/HEAD"),
        ],
    );
    let Some(name) = default
        .as_ref()
        .ok()
        .and_then(|d| d.strip_prefix(&format!("{remote}/")))
    else {
        return Ok(format!(
            "; default branch not checked ({remote}/HEAD unknown; run `git remote set-head {remote} --auto`)"
        ));
    };
    let default = format!("{remote}/{name}");
    if upstream == default {
        return Ok(String::new());
    }
    git(root, &["fetch", "--quiet", &remote, name])?;
    let ancestor = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["merge-base", "--is-ancestor", &default, "HEAD"])
        .output()?;
    if ancestor.status.code() == Some(1) {
        return Ok(format!(
            "; {name} not updated (it has commits this branch lacks)"
        ));
    }
    if !ancestor.status.success() {
        bail!("git merge-base failed");
    }
    let range = format!("{default}..HEAD");
    let others = other_commits(root, &range, relative)?;
    if others > 0 {
        return Ok(format!(
            "; {name} not updated ({others} other commits awaiting merge; merge {branch} into {name} to publish)"
        ));
    }
    if git(root, &["rev-list", "--count", &range])? == "0" {
        return Ok(format!("; {name} up to date"));
    }
    git(root, &["push", &remote, &format!("HEAD:refs/heads/{name}")])?;
    Ok(format!("; {name} fast-forwarded"))
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
