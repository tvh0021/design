//! Guarded replacement and git publication of public snapshots.
//!
//! Without pushing, the snapshot replaces the destination directory and, inside
//! a git repository, becomes a local commit that touches only that directory.
//! With pushing, the snapshot is committed directly on top of the remote's
//! default branch through a temporary index, whatever branch is checked out,
//! so unmerged local or project-branch commits never ship with it. `HEAD`, the
//! real index, and the working tree stay as they are, except that a checkout
//! of the default branch at its old tip is fast-forwarded to the new commit.
use anyhow::{Context, Result, bail};
use std::{
    ffi::OsStr,
    io::ErrorKind,
    path::{Component, Path, PathBuf},
    process::{Command, Output},
};

pub const MESSAGE: &str = "Refresh the public usage snapshot";
const INVALID: &str = "publish directory must be a regular directory below the repository root";

/// Git in `dir`, ignoring repository overrides inherited from the environment
/// (hooks export `GIT_DIR`) and reading pathspecs literally.
fn git_cmd(dir: &Path) -> Command {
    let mut cmd = Command::new("git");
    for var in [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_PREFIX",
    ] {
        cmd.env_remove(var);
    }
    cmd.env("GIT_LITERAL_PATHSPECS", "1").arg("-C").arg(dir);
    cmd
}

/// Git output with stdout as raw bytes; `None` when git exits non-zero.
fn git_output(dir: &Path, index: Option<&Path>, args: &[&str]) -> Result<Option<Output>> {
    let mut cmd = git_cmd(dir);
    cmd.args(args);
    if let Some(index) = index {
        cmd.env("GIT_INDEX_FILE", index);
    }
    let output = cmd.output()?;
    Ok(output.status.success().then_some(output))
}

fn git_with(dir: &Path, index: Option<&Path>, args: &[&str]) -> Result<String> {
    let Some(output) = git_output(dir, index, args)? else {
        // Git diagnostics can contain private paths or remote credentials.
        let name = args.iter().find(|a| !a.starts_with('-')).unwrap_or(&"");
        bail!("git {name} failed");
    };
    Ok(String::from_utf8(output.stdout)?.trim().to_owned())
}

fn git(dir: &Path, args: &[&str]) -> Result<String> {
    git_with(dir, None, args)
}

pub fn run(db: &Path, out: &Path, push: bool, now_ms: i64) -> Result<String> {
    // Rebuilding `out` from its name drops a trailing slash, which would follow
    // a symlink, and rejects `.`, `..` and `.git`.
    let Some(out_name) = out.file_name().filter(|n| *n != ".git") else {
        bail!(INVALID);
    };
    let parent = out
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let out = &parent.join(out_name);
    if out.join(".git").exists() || std::fs::symlink_metadata(out).is_ok_and(|m| !m.is_dir()) {
        bail!(INVALID);
    }
    // Validate against the deepest existing ancestor so nothing is created
    // before the destination is known to be safe. The walk refuses an existing
    // file or symlink on the way instead of treating it as a missing directory.
    let wanted = std::env::current_dir()?.join(parent);
    let mut existing = wanted.as_path();
    loop {
        match std::fs::symlink_metadata(existing) {
            Ok(_) if existing.is_dir() => break,
            Ok(_) => bail!(INVALID),
            Err(e) if e.kind() == ErrorKind::NotFound => {
                existing = existing.parent().context(INVALID)?;
            }
            Err(e) if e.kind() == ErrorKind::NotADirectory => bail!(INVALID),
            Err(e) => return Err(e.into()),
        }
    }
    let missing: PathBuf = wanted.strip_prefix(existing)?.components().collect();
    if missing
        .components()
        .any(|c| !matches!(c, Component::Normal(n) if n != ".git"))
    {
        bail!(INVALID);
    }
    let existing = std::fs::canonicalize(existing)?;
    let discovery = git_cmd(&existing)
        .args(["rev-parse", "--show-toplevel"])
        .env("LC_ALL", "C")
        .output()?;
    if !discovery.status.success() {
        let stderr = String::from_utf8_lossy(&discovery.stderr);
        if stderr.contains("not a git repository") {
            let (files, _) = replace_locally(db, out, now_ms, None)?;
            return Ok(format!(
                "publish: {files} files written; no commit; not pushed (not a git repository)"
            ));
        }
        if stderr.contains("must be run in a work tree") {
            bail!(INVALID);
        }
        bail!("git rev-parse failed");
    }
    let root = std::fs::canonicalize(String::from_utf8(discovery.stdout)?.trim())?;
    let out_path = existing.join(&missing).join(out_name);
    let relative = out_path
        .strip_prefix(&root)?
        .to_str()
        .context("non-UTF-8 publish directory")?;
    if relative.is_empty()
        || Path::new(relative)
            .components()
            .any(|c| c.as_os_str() == ".git")
    {
        bail!(INVALID);
    }
    let local = |reason: &str| -> Result<String> {
        let (files, changed) = replace_locally(db, out, now_ms, Some((&root, relative)))?;
        Ok(format!(
            "publish: {files} files written; {}; not pushed ({reason})",
            if changed { "committed" } else { "no commit" }
        ))
    };
    if !push {
        return local("disabled");
    }
    let remotes = git(&root, &["remote"])?;
    let remotes: Vec<&str> = remotes.lines().collect();
    let remote = match remotes.as_slice() {
        [] => return local("no remote"),
        _ if remotes.contains(&"origin") => "origin",
        [only] => only,
        _ => bail!("multiple git remotes configured; expected origin"),
    };
    let default = git(
        &root,
        &[
            "symbolic-ref",
            "--short",
            &format!("refs/remotes/{remote}/HEAD"),
        ],
    );
    let Some(default) = default
        .as_ref()
        .ok()
        .and_then(|d| d.strip_prefix(&format!("{remote}/")))
    else {
        bail!("{remote}/HEAD unknown; run `git remote set-head {remote} --auto`");
    };
    deploy(db, &root, relative, remote, default, now_ms)
}

/// Writes the snapshot into `out` and, inside a repository, commits it there.
/// Returns the file count and whether a commit was made.
fn replace_locally(
    db: &Path,
    out: &Path,
    now_ms: i64,
    repo: Option<(&Path, &str)>,
) -> Result<(usize, bool)> {
    // Stage beside `out` so the final renames stay on one filesystem.
    let parent = out.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(parent)?;
    let stage = tempfile::Builder::new()
        .prefix(".usage-publish-")
        .tempdir_in(parent)?;
    let stage = stage.path();
    let pages = stage.join("pages");
    let files = crate::serve::snapshot::write(db, &pages, None, now_ms)?;
    retain_timing(&pages, |name| std::fs::read_to_string(out.join(name)).ok())?;
    guard(db)?(&pages)?;
    // Refuse to include someone else's staged work in our commit.
    if let Some((root, _)) = repo
        && !git(root, &["diff", "--cached", "--name-only"])?.is_empty()
    {
        bail!("git index already has staged changes");
    }
    // Keep the previous tree until the guarded replacement succeeds.
    let backup = stage.join("previous");
    if out.exists() {
        std::fs::rename(out, &backup)?;
    }
    if let Err(e) = std::fs::rename(&pages, out) {
        if backup.exists() {
            std::fs::rename(&backup, out)?;
        }
        return Err(e.into());
    }
    let Some((root, relative)) = repo else {
        return Ok((files, false));
    };
    git(root, &["add", "--", relative])?;
    let changed = !git(root, &["diff", "--cached", "--name-only", "--", relative])?.is_empty();
    if changed {
        git(root, &["commit", "-m", MESSAGE])?;
    }
    Ok((files, changed))
}

/// Commits the snapshot on top of `<remote>/<default>` with a temporary index
/// and pushes it as a fast-forward.
fn deploy(
    db: &Path,
    root: &Path,
    relative: &str,
    remote: &str,
    default: &str,
    now_ms: i64,
) -> Result<String> {
    // Deploying writes nothing to the working tree, so stage in the temp dir.
    let stage = tempfile::Builder::new()
        .prefix(".usage-publish-")
        .tempdir()?;
    let stage = &std::fs::canonicalize(stage.path())?;
    let tracking = format!("refs/remotes/{remote}/{default}");
    let refspec = format!("+refs/heads/{default}:{tracking}");
    let fetch = || -> Result<String> {
        git(root, &["fetch", "--quiet", "--no-tags", remote, &refspec])?;
        git(
            root,
            &["rev-parse", "--verify", &format!("{tracking}^{{commit}}")],
        )
    };
    let mut base = fetch()?;
    let raw = stage.join("raw");
    let files = crate::serve::snapshot::write(db, &raw, None, now_ms)?;
    let check = guard(db)?;
    check(&raw)?;
    let tree_root = stage.join("tree");
    let tree_root_arg = format!(
        "--work-tree={}",
        tree_root.to_str().context("non-UTF-8 path")?
    );
    let dest = tree_root.join(relative);
    let index = stage.join("index");
    let sign = git(root, &["config", "--type=bool", "commit.gpgsign"]).is_ok_and(|v| v == "true");
    let mut attempt = 0;
    let commit = loop {
        refuse_tracked_file(root, &base, relative)?;
        if dest.exists() {
            std::fs::remove_dir_all(&dest)?;
        }
        std::fs::create_dir_all(&dest)?;
        for entry in std::fs::read_dir(&raw)? {
            let entry = entry?;
            std::fs::copy(entry.path(), dest.join(entry.file_name()))?;
        }
        retain_timing(&dest, |name| {
            let spec = format!("{base}:{relative}/{}", name.to_str()?);
            let output = git_output(root, None, &["cat-file", "blob", &spec]).ok()??;
            String::from_utf8(output.stdout).ok()
        })?;
        check(&dest)?;
        if index.exists() {
            std::fs::remove_file(&index)?;
        }
        git_with(root, Some(&index), &["read-tree", &base])?;
        git_with(
            root,
            Some(&index),
            &[&tree_root_arg, "add", "--all", "--force", "--", relative],
        )?;
        let tree = git_with(root, Some(&index), &["write-tree"])?;
        let base_tree = git(
            root,
            &["rev-parse", "--verify", &format!("{base}^{{tree}}")],
        )?;
        if tree == base_tree {
            return Ok(format!(
                "publish: {files} files written; no commit; {remote}/{default} up to date"
            ));
        }
        let mut args = vec!["commit-tree", tree.as_str()];
        if sign {
            args.push("-S");
        }
        args.extend(["-p", base.as_str(), "-m", MESSAGE]);
        let commit = git(root, &args)?;
        let target = format!("{commit}:refs/heads/{default}");
        if git(root, &["push", "--quiet", remote, &target]).is_ok() {
            break commit;
        }
        // Another push may have moved the default branch; rebuild on its tip.
        let Ok(latest) = fetch() else {
            bail!("git push failed");
        };
        if latest == base || attempt == 2 {
            bail!("git push failed");
        }
        base = latest;
        attempt += 1;
    };
    git(root, &["update-ref", &tracking, &commit])?;
    let follow = git(root, &["symbolic-ref", "--quiet", "--short", "HEAD"])
        .is_ok_and(|b| b == default)
        && git(
            root,
            &["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        )
        .is_ok_and(|u| u == format!("{remote}/{default}"))
        && git(root, &["rev-parse", "--verify", "HEAD"]).is_ok_and(|h| h == base);
    let suffix = if !follow {
        String::new()
    } else if git(root, &["merge", "--ff-only", "--quiet", &commit]).is_ok() {
        format!("; local {default} fast-forwarded")
    } else {
        format!("; local {default} not fast-forwarded (git merge failed)")
    };
    Ok(format!(
        "publish: {files} files written; committed; pushed to {remote}/{default}{suffix}"
    ))
}

/// Refuses a destination that `base` tracks as a file or submodule, at the
/// destination itself or at any directory above it.
fn refuse_tracked_file(root: &Path, base: &str, relative: &str) -> Result<()> {
    let mut prefix = String::new();
    for part in relative.split('/') {
        if !prefix.is_empty() {
            prefix.push('/');
        }
        prefix.push_str(part);
        let spec = format!("{base}:{prefix}");
        match git_output(root, None, &["cat-file", "-t", &spec])? {
            None => return Ok(()),
            Some(output) if output.stdout.trim_ascii() == b"tree" => {}
            Some(_) => bail!(INVALID),
        }
    }
    Ok(())
}

/// The privacy guard for a directory of generated pages.
fn guard(db: &Path) -> Result<impl Fn(&Path) -> Result<()>> {
    let conn = crate::serve::open_read_only(db)?.context("missing usage store")?;
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let public_projects = crate::config::load()?.public_projects;
    Ok(move |pages: &Path| {
        crate::privacy::check(
            &conn,
            pages,
            home.as_deref(),
            &public_projects,
            &Default::default(),
        )
    })
}

/// Keeps a previous page body when only its generation clock and load timing
/// differ from the new one.
fn retain_timing(pages: &Path, previous: impl Fn(&OsStr) -> Option<String>) -> Result<()> {
    for entry in std::fs::read_dir(pages)? {
        let entry = entry?;
        if let Some(previous) = previous(&entry.file_name()) {
            let next = std::fs::read_to_string(entry.path())?;
            if without_timing(&previous) == without_timing(&next) {
                std::fs::write(entry.path(), previous)?;
            }
        }
    }
    Ok(())
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
