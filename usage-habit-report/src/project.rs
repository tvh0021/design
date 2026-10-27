//! Projects: each session belongs to the git toplevel of its working directory
//! (worktrees collapse into their main repository), or to the directory itself
//! outside a repository. Resolution runs as the `enrich` step after ingest;
//! this module also holds the read side (`query project`, `query projects`).

use crate::query::{Tokens, rfc3339};
use crate::t3;
use anyhow::Result;
use rusqlite::{Connection, params};
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// The project a working directory belongs to.
///
/// - Inside a repository: its toplevel (the nearest ancestor holding `.git`).
///   For a linked worktree (`.git` is a file pointing into
///   `<main>/.git/worktrees/<name>`), the main repository's toplevel, so every
///   worktree of a repo is one project. Submodules stay their own project.
/// - Outside a repository: the directory itself.
/// - A path that no longer exists: the string as recorded, except that a path
///   inside `<repo>/.claude/worktrees/<name>` or `<repo>/.git/worktrees/<name>`
///   (a deleted worktree) is resolved as `<repo>` (see [`enclosing_repo`]).
///
/// Existing paths are canonicalized first (symlinks resolved).
pub fn resolve(cwd: &str) -> String {
    let Ok(dir) = std::fs::canonicalize(cwd) else {
        return match enclosing_repo(cwd) {
            Some(repo) => resolve(repo),
            None => cwd.to_string(),
        };
    };
    for d in dir.ancestors() {
        let dot_git = d.join(".git");
        let Ok(meta) = std::fs::symlink_metadata(&dot_git) else {
            continue;
        };
        if meta.is_dir() {
            return d.to_string_lossy().into_owned();
        }
        if meta.is_file() {
            let main = main_toplevel(d, &dot_git);
            return main
                .unwrap_or_else(|| d.to_path_buf())
                .to_string_lossy()
                .into_owned();
        }
    }
    dir.to_string_lossy().into_owned()
}

/// For a path that no longer exists, the repository path it was a worktree
/// of: everything before a `/.claude/worktrees/<name>` or
/// `/.git/worktrees/<name>` component (the `.git` itself is dropped in the
/// second form). None when the path has neither marker, or the marker is at
/// the very start so nothing would be left.
fn enclosing_repo(path: &str) -> Option<&str> {
    ["/.claude/worktrees/", "/.git/worktrees/"]
        .iter()
        .find_map(|marker| path.find(marker).map(|i| &path[..i]))
        .filter(|repo| !repo.is_empty())
}

/// For a `.git` file (`gitdir: <path>`), the main worktree's toplevel when the
/// gitdir is a linked worktree's admin folder (it has a `commondir` file and
/// the common dir is a `.git` folder). None for submodules, bare-repo
/// worktrees, and anything unreadable.
fn main_toplevel(toplevel: &Path, dot_git: &Path) -> Option<PathBuf> {
    let text = std::fs::read_to_string(dot_git).ok()?;
    let gitdir = text.lines().find_map(|l| l.strip_prefix("gitdir:"))?.trim();
    let gitdir = toplevel.join(gitdir);
    let common = std::fs::read_to_string(gitdir.join("commondir")).ok()?;
    let common = std::fs::canonicalize(gitdir.join(common.trim())).ok()?;
    if common.file_name()? != ".git" {
        return None; // bare repository: no main worktree to collapse into
    }
    common.parent().map(Path::to_path_buf)
}

/// Explicit umbrella projects and T3's recorded worktree ownership. Filesystem
/// boundaries prevent an alias for `arena` from matching `arena-other`.
///
/// `all` holds both kinds, configuration winning for the same source;
/// `configured` holds only `config.toml` mappings, which are applied once more
/// to a mapping's target so recorded worktree ownership folds into a
/// configured umbrella.
#[derive(Default)]
struct Aliases {
    all: BTreeMap<String, String>,
    configured: BTreeMap<String, String>,
}

impl Aliases {
    fn load(t3_paths: &[PathBuf]) -> Result<Self> {
        let mut out = BTreeMap::new();
        for path in t3_paths.iter().filter(|p| p.is_file()) {
            match t3::project_aliases(path) {
                Ok(aliases) => {
                    for (source, target) in aliases {
                        out.entry(source).or_insert(target);
                    }
                }
                Err(e) => eprintln!(
                    "notice: cannot read project ownership from {}: {e:#}",
                    path.display()
                ),
            }
        }
        // User configuration takes priority over recorded ownership.
        let configured = canonical_sources(crate::config::load()?.project_aliases);
        out.extend(configured.clone());
        Ok(Self {
            all: canonical_sources(out),
            configured,
        })
    }

    /// The project for a working directory: the most specific owner's target,
    /// resolved through the Git rules. A recorded (T3) owner's target is first
    /// folded into a configured umbrella when one contains it; configured
    /// mappings themselves never chain.
    fn resolve(&self, cwd: &str) -> String {
        let Some((source, owned)) = owner(&self.all, cwd) else {
            return resolve(cwd);
        };
        if self.configured.contains_key(source) {
            return resolve(owned);
        }
        resolve(owner(&self.configured, owned).map_or(owned, |(_, t)| t))
    }
}

/// Alias sources with symlinks resolved, so they compare with canonical cwds.
fn canonical_sources(aliases: BTreeMap<String, String>) -> BTreeMap<String, String> {
    aliases
        .into_iter()
        .map(|(source, target)| {
            let source = std::fs::canonicalize(&source).unwrap_or_else(|_| PathBuf::from(source));
            (source.to_string_lossy().into_owned(), target)
        })
        .collect()
}

/// The most specific alias whose source contains `path`, as (source, target).
fn owner<'a>(aliases: &'a BTreeMap<String, String>, path: &str) -> Option<(&'a str, &'a str)> {
    let dir = std::fs::canonicalize(path).unwrap_or_else(|_| PathBuf::from(path));
    aliases
        .iter()
        .filter(|(source, _)| dir.starts_with(source))
        .max_by_key(|(source, _)| Path::new(source).components().count())
        .map(|(source, target)| (source.as_str(), target.as_str()))
}

/// Last path component, or the whole string for `/` and the like.
pub fn project_name(project: &str) -> String {
    Path::new(project)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| project.to_string())
}

#[derive(Debug, Default, Clone)]
pub struct EnrichReport {
    pub t3: t3::T3Report,
    /// Sessions that got a project this run.
    pub projects_set: u64,
    /// Of those, sessions whose directory came from T3 (no recorded cwd).
    pub projects_from_t3: u64,
    /// Sessions still without a project because no directory is known.
    pub without_cwd: u64,
    pub elapsed: Duration,
}

/// The enrich step: attach T3 thread ids and titles, then resolve the project
/// of every session, refreshing historical assignments. Missing T3 databases
/// are skipped; ones that fail to open or read get one notice on stderr.
pub fn enrich(conn: &mut Connection, t3_paths: &[PathBuf]) -> Result<EnrichReport> {
    let start = Instant::now();
    let mut report = EnrichReport::default();
    let mut links = t3::Links::new();
    for p in t3_paths {
        if !p.is_file() {
            continue;
        }
        match t3::load(p) {
            Ok(l) => {
                report.t3.databases_read += 1;
                t3::merge(&mut links, l);
            }
            Err(e) => {
                report.t3.databases_unreadable += 1;
                eprintln!(
                    "notice: T3 state database {} unreadable, skipping T3 links: {e:#}",
                    p.display()
                );
            }
        }
    }

    let aliases = Aliases::load(t3_paths)?;
    let tx = conn.transaction()?;
    report.t3.sessions_linked = t3::apply(&tx, &links)?;

    // Revisit recorded assignments so new aliases also fix historical usage.
    type Pending = (i64, String, String, Option<String>, Option<String>);
    let pending: Vec<Pending> = tx
        .prepare("SELECT id, tool, session_id, cwd, project FROM sessions")?
        .query_map([], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        })?
        .collect::<rusqlite::Result<_>>()?;
    let mut cache: HashMap<String, String> = HashMap::new();
    {
        let mut update =
            tx.prepare("UPDATE sessions SET project = ?2, project_name = ?3 WHERE id = ?1")?;
        for (id, tool, session_id, cwd, old_project) in pending {
            let (dir, from_t3) = match cwd.filter(|c| !c.is_empty()) {
                Some(c) => (c, false),
                None => match links
                    .get(&(tool, session_id))
                    .and_then(|l| l.workspace.clone())
                {
                    Some(w) => (w, true),
                    None => match old_project.clone() {
                        Some(p) => (p, false),
                        None => {
                            report.without_cwd += 1;
                            continue;
                        }
                    },
                },
            };
            let mut project = cache
                .entry(dir.clone())
                .or_insert_with_key(|d| aliases.resolve(d))
                .clone();
            // Keep a previously established Git root if its deleted cwd has
            // no current ownership evidence. Never invent a new project for it.
            if project == dir
                && !Path::new(&dir).exists()
                && let Some(old) = &old_project
            {
                project = aliases.resolve(old);
            }
            if old_project.as_deref() == Some(project.as_str()) {
                continue;
            }
            update.execute(params![id, project, project_name(&project)])?;
            report.projects_set += 1;
            report.projects_from_t3 += from_t3 as u64;
        }
    }
    t3::enrich_lineage(&tx, t3_paths)?;
    tx.commit()?;
    report.elapsed = start.elapsed();
    Ok(report)
}

/// One project's totals. `project` and `name` are null for the row that
/// collects sessions with no known directory.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ProjectRow {
    pub project: Option<String>,
    pub name: Option<String>,
    pub sessions: i64,
    pub first_request_at: Option<String>,
    pub last_request_at: Option<String>,
    #[serde(flatten)]
    pub tokens: Tokens,
    pub active_seconds: i64,
    pub elapsed_seconds: i64,
    pub idle_gap_seconds: i64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ToolRow {
    pub tool: String,
    pub sessions: i64,
    pub first_request_at: Option<String>,
    pub last_request_at: Option<String>,
    #[serde(flatten)]
    pub tokens: Tokens,
    pub active_seconds: i64,
    pub elapsed_seconds: i64,
    pub idle_gap_seconds: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct ProjectDetail {
    pub generated_at: String,
    pub since: Option<String>,
    pub rates_fetched_at: Option<String>,
    #[serde(flatten)]
    pub project: ProjectRow,
    pub tools: Vec<ToolRow>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ProjectList {
    pub generated_at: String,
    pub since: Option<String>,
    pub rates_fetched_at: Option<String>,
    pub projects: Vec<ProjectRow>,
}

/// Raw aggregate for one (project, tool) cell.
#[derive(Debug, Default, Clone)]
struct Cell {
    sessions: i64,
    active_seconds: i64,
    elapsed_seconds: i64,
    first_ms: Option<i64>,
    last_ms: Option<i64>,
    tokens: Tokens,
}

fn add_tokens(a: &mut Tokens, b: &Tokens) {
    a.requests += b.requests;
    a.input_tokens += b.input_tokens;
    a.cache_read_input_tokens += b.cache_read_input_tokens;
    a.cache_creation_input_tokens += b.cache_creation_input_tokens;
    a.output_tokens += b.output_tokens;
    a.unpriced_requests += b.unpriced_requests;
    if let Some(c) = b.estimated_cost_usd {
        *a.estimated_cost_usd.get_or_insert(0.0) += c;
    }
}

fn add_cell(a: &mut Cell, b: &Cell) {
    a.sessions += b.sessions;
    a.active_seconds += b.active_seconds;
    a.elapsed_seconds += b.elapsed_seconds;
    a.first_ms = match (a.first_ms, b.first_ms) {
        (Some(x), Some(y)) => Some(x.min(y)),
        (x, y) => x.or(y),
    };
    a.last_ms = a.last_ms.max(b.last_ms);
    add_tokens(&mut a.tokens, &b.tokens);
}

/// Aggregates per (project, tool). With `since_ms`, sessions count when they
/// ended (or, lacking an end, started) at or after it, and requests when their
/// timestamp is; without it, everything counts.
/// `only` restricts to one project.
fn cells(
    conn: &Connection,
    since_ms: Option<i64>,
    only: Option<&str>,
) -> Result<BTreeMap<(Option<String>, String), Cell>> {
    let since = since_ms.unwrap_or(i64::MIN);
    let mut out: BTreeMap<(Option<String>, String), Cell> = BTreeMap::new();
    let mut stmt = conn.prepare(
        r#"SELECT project, tool, COUNT(*),
                  COALESCE(SUM(CASE WHEN NOT EXISTS (
                      SELECT 1 FROM sessions p WHERE p.tool = COALESCE(sessions.parent_tool, sessions.tool)
                      AND p.session_id = sessions.parent_session_id)
                      THEN active_ms / 1000 ELSE 0 END), 0),
                  COALESCE(SUM(CASE WHEN NOT EXISTS (
                      SELECT 1 FROM sessions p WHERE p.tool = COALESCE(sessions.parent_tool, sessions.tool)
                      AND p.session_id = sessions.parent_session_id)
                      THEN MAX(0, (ended_at_ms - started_at_ms) / 1000) ELSE 0 END), 0)
           FROM sessions
           WHERE (?3 OR COALESCE(ended_at_ms, started_at_ms) >= ?1)
             AND (?2 IS NULL OR project = ?2)
           GROUP BY project, tool"#,
    )?;
    let mut rows = stmt.query(params![since, only, since_ms.is_none()])?;
    while let Some(r) = rows.next()? {
        let c = out.entry((r.get(0)?, r.get(1)?)).or_default();
        c.sessions = r.get(2)?;
        c.active_seconds = r.get(3)?;
        c.elapsed_seconds = r.get(4)?;
    }
    // Requests are added up per session first, so the join to `sessions`
    // runs once per session rather than once per request.
    let mut stmt = conn.prepare(
        r#"WITH r(tool, session_id, n, inp, cr, cc, outp, cost, unpriced, f, l) AS (
               SELECT tool, session_id, COUNT(*),
                      SUM(input_tokens), SUM(cache_read_input_tokens),
                      SUM(cache_creation_input_tokens), SUM(output_tokens),
                      SUM(cost_usd), COUNT(*) - COUNT(cost_usd),
                      MIN(ts_ms), MAX(ts_ms)
               FROM requests WHERE ts_ms >= ?1
               GROUP BY tool, session_id
           )
           SELECT s.project, r.tool, SUM(r.n),
                  SUM(r.inp), SUM(r.cr), SUM(r.cc), SUM(r.outp),
                  SUM(r.cost), SUM(r.unpriced), MIN(r.f), MAX(r.l)
           FROM r
           LEFT JOIN sessions s ON s.tool = r.tool AND s.session_id = r.session_id
           WHERE ?2 IS NULL OR s.project = ?2
           GROUP BY s.project, r.tool"#,
    )?;
    let mut rows = stmt.query(params![since, only])?;
    while let Some(r) = rows.next()? {
        let c = out.entry((r.get(0)?, r.get(1)?)).or_default();
        c.tokens = Tokens {
            requests: r.get(2)?,
            input_tokens: r.get(3)?,
            cache_read_input_tokens: r.get(4)?,
            cache_creation_input_tokens: r.get(5)?,
            output_tokens: r.get(6)?,
            estimated_cost_usd: r.get(7)?,
            unpriced_requests: r.get(8)?,
        };
        c.first_ms = r.get(9)?;
        c.last_ms = r.get(10)?;
    }
    Ok(out)
}

fn total_tokens(t: &Tokens) -> i64 {
    t.input_tokens + t.cache_read_input_tokens + t.cache_creation_input_tokens + t.output_tokens
}

fn project_row(project: Option<String>, c: Cell) -> ProjectRow {
    ProjectRow {
        name: project.as_deref().map(project_name),
        project,
        sessions: c.sessions,
        active_seconds: c.active_seconds,
        elapsed_seconds: c.elapsed_seconds,
        idle_gap_seconds: crate::store::IDLE_GAP_SECONDS,
        first_request_at: c.first_ms.map(rfc3339),
        last_request_at: c.last_ms.map(rfc3339),
        tokens: c.tokens,
    }
}

/// Every project with totals, largest token total first. Sessions without a
/// project are collected in one row with null `project` and `name`.
pub fn list(conn: &Connection, since_ms: Option<i64>, now_ms: i64) -> Result<ProjectList> {
    list_for(conn, since_ms, None, now_ms)
}

/// [`list`] counting only one tool's sessions and requests (all tools when
/// `tool` is None).
pub fn list_for(
    conn: &Connection,
    since_ms: Option<i64>,
    tool: Option<&str>,
    now_ms: i64,
) -> Result<ProjectList> {
    Ok(list_with_tools(conn, since_ms, tool, now_ms)?.0)
}

/// Per-(project, tool) rows, keyed by project path (None: no known directory).
pub type ProjectTools = Vec<(Option<String>, ToolRow)>;

/// [`list_for`] plus the per-(project, tool) rows it adds up, ordered by
/// project then tool, in one pass over the store.
pub fn list_with_tools(
    conn: &Connection,
    since_ms: Option<i64>,
    tool: Option<&str>,
    now_ms: i64,
) -> Result<(ProjectList, ProjectTools)> {
    let mut by_project: BTreeMap<Option<String>, Cell> = BTreeMap::new();
    let mut tools = Vec::new();
    for ((project, t), c) in cells(conn, since_ms, None)? {
        if tool.is_some_and(|want| want != t) {
            continue;
        }
        add_cell(by_project.entry(project.clone()).or_default(), &c);
        tools.push((
            project,
            ToolRow {
                tool: t,
                sessions: c.sessions,
                active_seconds: c.active_seconds,
                elapsed_seconds: c.elapsed_seconds,
                idle_gap_seconds: crate::store::IDLE_GAP_SECONDS,
                first_request_at: c.first_ms.map(rfc3339),
                last_request_at: c.last_ms.map(rfc3339),
                tokens: c.tokens,
            },
        ));
    }
    let mut projects: Vec<ProjectRow> = by_project
        .into_iter()
        .map(|(p, c)| project_row(p, c))
        .collect();
    projects.sort_by(|a, b| {
        total_tokens(&b.tokens)
            .cmp(&total_tokens(&a.tokens))
            .then(b.sessions.cmp(&a.sessions))
            .then(a.project.cmp(&b.project))
    });
    Ok((
        ProjectList {
            generated_at: rfc3339(now_ms),
            since: since_ms.map(rfc3339),
            rates_fetched_at: None,
            projects,
        },
        tools,
    ))
}

/// Totals for one project (an exact `sessions.project` value) with a per-tool
/// breakdown, largest token total first.
pub fn detail(
    conn: &Connection,
    project: &str,
    since_ms: Option<i64>,
    now_ms: i64,
) -> Result<ProjectDetail> {
    let mut total = Cell::default();
    let mut tools = Vec::new();
    for ((_, tool), c) in cells(conn, since_ms, Some(project))? {
        add_cell(&mut total, &c);
        tools.push(ToolRow {
            tool,
            sessions: c.sessions,
            active_seconds: c.active_seconds,
            elapsed_seconds: c.elapsed_seconds,
            idle_gap_seconds: crate::store::IDLE_GAP_SECONDS,
            first_request_at: c.first_ms.map(rfc3339),
            last_request_at: c.last_ms.map(rfc3339),
            tokens: c.tokens,
        });
    }
    tools.sort_by(|a, b| {
        total_tokens(&b.tokens)
            .cmp(&total_tokens(&a.tokens))
            .then(a.tool.cmp(&b.tool))
    });
    Ok(ProjectDetail {
        generated_at: rfc3339(now_ms),
        since: since_ms.map(rfc3339),
        rates_fetched_at: None,
        project: project_row(Some(project.to_string()), total),
        tools,
    })
}

/// A project that matched a key, for the ambiguous-match listing.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Candidate {
    pub project: String,
    pub name: String,
    pub sessions: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Lookup {
    Found(String),
    Ambiguous(Vec<Candidate>),
    NotFound,
}

/// Find a project by key: exact stored path; then, when the key is an existing
/// directory, the project it resolves to; then folder name, case-insensitive.
pub fn lookup(conn: &Connection, key: &str) -> Result<Lookup> {
    let key = match key.trim_end_matches('/') {
        "" => key,
        k => k,
    };
    let all: Vec<Candidate> = conn
        .prepare(
            "SELECT project, COALESCE(project_name, ''), COUNT(*) FROM sessions
             WHERE project IS NOT NULL GROUP BY project ORDER BY project",
        )?
        .query_map([], |r| {
            Ok(Candidate {
                project: r.get(0)?,
                name: r.get(1)?,
                sessions: r.get(2)?,
            })
        })?
        .collect::<rusqlite::Result<_>>()?;
    if all.iter().any(|c| c.project == key) {
        return Ok(Lookup::Found(key.to_string()));
    }
    if Path::new(key).is_absolute() || Path::new(key).exists() {
        let resolved = Aliases::load(&t3::default_paths())?.resolve(key);
        if all.iter().any(|c| c.project == resolved) {
            return Ok(Lookup::Found(resolved));
        }
    }
    let lower = key.to_lowercase();
    let mut hits: Vec<Candidate> = all
        .into_iter()
        .filter(|c| c.name.to_lowercase() == lower)
        .collect();
    Ok(match hits.len() {
        0 => Lookup::NotFound,
        1 => Lookup::Found(hits.remove(0).project),
        _ => Lookup::Ambiguous(hits),
    })
}

fn cost_cell(t: &Tokens) -> String {
    match t.estimated_cost_usd {
        Some(c) if t.unpriced_requests > 0 => format!("${c:.2}*"),
        Some(c) => format!("${c:.2}"),
        None => "-".into(),
    }
}

fn day(ts: &Option<String>) -> &str {
    ts.as_deref().map_or("-", |s| s.get(..10).unwrap_or(s))
}

/// Left-align the first `left` columns, right-align the rest.
pub(crate) fn table(rows: &[Vec<String>], left: usize) -> String {
    let n = rows.first().map_or(0, Vec::len);
    let widths: Vec<usize> = (0..n)
        .map(|i| rows.iter().map(|r| r[i].chars().count()).max().unwrap_or(0))
        .collect();
    let mut out = String::new();
    for r in rows {
        for (i, cell) in r.iter().enumerate() {
            if i < left {
                out.push_str(&format!("{:<w$}  ", cell, w = widths[i]));
            } else {
                out.push_str(&format!("{:>w$}  ", cell, w = widths[i]));
            }
        }
        out.truncate(out.trim_end().len());
        out.push('\n');
    }
    out
}

const TOKEN_HEADER: [&str; 6] = [
    "requests",
    "input",
    "cache_read",
    "cache_create",
    "output",
    "est. cost",
];

fn token_cells(t: &Tokens) -> [String; 6] {
    [
        t.requests.to_string(),
        t.input_tokens.to_string(),
        t.cache_read_input_tokens.to_string(),
        t.cache_creation_input_tokens.to_string(),
        t.output_tokens.to_string(),
        cost_cell(t),
    ]
}

pub fn render_list_table(l: &ProjectList) -> String {
    let mut rows = vec![
        ["name", "sessions", "active (s)", "first", "last"]
            .iter()
            .chain(TOKEN_HEADER.iter())
            .chain(["project"].iter())
            .map(|s| s.to_string())
            .collect::<Vec<_>>(),
    ];
    for p in &l.projects {
        let mut r = vec![
            p.name.clone().unwrap_or_else(|| "(no directory)".into()),
            p.sessions.to_string(),
            p.active_seconds.to_string(),
            day(&p.first_request_at).to_string(),
            day(&p.last_request_at).to_string(),
        ];
        r.extend(token_cells(&p.tokens));
        r.push(p.project.clone().unwrap_or_default());
        rows.push(r);
    }
    // Every column but the name and the trailing path is right-aligned; pad the
    // path column by hand so it stays left-aligned.
    let mut out = format!("since: {}\n", l.since.as_deref().unwrap_or("all time"));
    let paths: Vec<String> = rows
        .iter_mut()
        .map(|r| r.pop().unwrap_or_default())
        .collect();
    for (line, path) in table(&rows, 1).lines().zip(paths) {
        out.push_str(line);
        out.push_str("  ");
        out.push_str(&path);
        out.truncate(out.trim_end().len());
        out.push('\n');
    }
    out
}

pub fn render_detail_table(d: &ProjectDetail) -> String {
    let p = &d.project;
    let mut out = format!(
        "project: {}\nname: {}\nsince: {}\nsessions: {}\nrequests: {} .. {}\n",
        p.project.as_deref().unwrap_or(""),
        p.name.as_deref().unwrap_or(""),
        d.since.as_deref().unwrap_or("all time"),
        p.sessions,
        p.first_request_at.as_deref().unwrap_or("-"),
        p.last_request_at.as_deref().unwrap_or("-"),
    );
    let mut rows = vec![
        ["tool", "sessions", "active (s)"]
            .iter()
            .chain(TOKEN_HEADER.iter())
            .map(|s| s.to_string())
            .collect::<Vec<_>>(),
    ];
    for t in &d.tools {
        let mut r = vec![
            t.tool.clone(),
            t.sessions.to_string(),
            t.active_seconds.to_string(),
        ];
        r.extend(token_cells(&t.tokens));
        rows.push(r);
    }
    let mut total = vec![
        "TOTAL".to_string(),
        p.sessions.to_string(),
        p.active_seconds.to_string(),
    ];
    total.extend(token_cells(&p.tokens));
    rows.push(total);
    out.push_str(&table(&rows, 1));
    out
}

pub fn render_candidates_table(key: &str, c: &[Candidate]) -> String {
    let mut out = format!("ambiguous: {} projects are named {key:?}\n", c.len());
    let mut rows = vec![vec!["project".to_string(), "sessions".to_string()]];
    for x in c {
        rows.push(vec![x.project.clone(), x.sessions.to_string()]);
    }
    out.push_str(&table(&rows, 1));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    fn git_init(dir: &Path) {
        let ok = Command::new("git")
            .args(["init", "-q"])
            .arg(dir)
            .status()
            .unwrap()
            .success();
        assert!(ok);
    }

    fn canon(p: &Path) -> String {
        std::fs::canonicalize(p)
            .unwrap()
            .to_string_lossy()
            .into_owned()
    }

    #[test]
    fn aliases_choose_the_most_specific_owner_without_matching_siblings() {
        let aliases = Aliases {
            all: BTreeMap::from([
                ("/missing/arena".into(), "/missing/arena".into()),
                ("/missing/arena/independent".into(), "/missing/other".into()),
            ]),
            configured: BTreeMap::new(),
        };
        assert_eq!(
            aliases.resolve("/missing/arena/runs/seat-02"),
            "/missing/arena"
        );
        assert_eq!(
            aliases.resolve("/missing/arena/independent/src"),
            "/missing/other"
        );
        assert_eq!(
            aliases.resolve("/missing/arena-other"),
            "/missing/arena-other"
        );
    }

    #[test]
    fn recorded_ownership_folds_into_a_configured_umbrella() {
        // T3 maps a worktree to its repository; config maps that repository
        // (and a sibling checkout) to one umbrella. The worktree lands in the
        // umbrella too, but configured mappings never chain into each other.
        let configured = BTreeMap::from([
            ("/missing/repo".into(), "/missing/umbrella".into()),
            ("/missing/other".into(), "/missing/umbrella".into()),
            ("/missing/umbrella".into(), "/missing/elsewhere".into()),
        ]);
        let mut all = configured.clone();
        all.insert("/missing/repo-worktrees/wt1".into(), "/missing/repo".into());
        let aliases = Aliases { all, configured };
        assert_eq!(
            aliases.resolve("/missing/repo-worktrees/wt1/src"),
            "/missing/umbrella"
        );
        assert_eq!(aliases.resolve("/missing/other/x"), "/missing/umbrella");
        assert_eq!(aliases.resolve("/missing/umbrella/x"), "/missing/elsewhere");
        assert_eq!(aliases.resolve("/missing/unrelated"), "/missing/unrelated");
    }

    #[test]
    fn reindex_preserves_known_ownership_after_cwd_disappears() {
        let mut store = crate::store::Store::open_in_memory().unwrap();
        store
            .conn
            .execute(
                "INSERT INTO sessions (tool, session_id, cwd, project, project_name)
             VALUES ('codex', 'gone', '/missing/checkout/src', '/missing/main', 'main')",
                [],
            )
            .unwrap();
        assert_eq!(enrich(&mut store.conn, &[]).unwrap().projects_set, 0);
        let project: String = store
            .conn
            .query_row(
                "SELECT project FROM sessions WHERE session_id = 'gone'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(project, "/missing/main");
    }

    #[test]
    fn repo_subdirectory_resolves_to_toplevel() {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("alpha");
        git_init(&repo);
        let sub = repo.join("src/deep");
        std::fs::create_dir_all(&sub).unwrap();
        assert_eq!(resolve(sub.to_str().unwrap()), canon(&repo));
        assert_eq!(resolve(repo.to_str().unwrap()), canon(&repo));
    }

    #[test]
    fn worktree_collapses_into_main_repo() {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("main-repo");
        git_init(&repo);
        let git = |args: &[&str]| {
            let out = Command::new("git")
                .arg("-C")
                .arg(&repo)
                .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
                .args(args)
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
        };
        git(&["commit", "-q", "--allow-empty", "-m", "init"]);
        let wt = tmp.path().join("main-repo-wt");
        git(&["worktree", "add", "-q", wt.to_str().unwrap(), "-b", "side"]);
        assert!(wt.join(".git").is_file());
        let sub = wt.join("nested");
        std::fs::create_dir_all(&sub).unwrap();
        assert_eq!(resolve(wt.to_str().unwrap()), canon(&repo));
        assert_eq!(resolve(sub.to_str().unwrap()), canon(&repo));
    }

    #[test]
    fn gitdir_file_without_commondir_is_its_own_project() {
        // Submodule layout: `.git` file points at `<super>/.git/modules/<name>`.
        let tmp = tempfile::tempdir().unwrap();
        let sup = tmp.path().join("super");
        git_init(&sup);
        let modules = sup.join(".git/modules/child");
        std::fs::create_dir_all(&modules).unwrap();
        let child = sup.join("child");
        std::fs::create_dir_all(&child).unwrap();
        std::fs::write(child.join(".git"), "gitdir: ../.git/modules/child\n").unwrap();
        assert_eq!(resolve(child.to_str().unwrap()), canon(&child));
    }

    #[test]
    fn non_repo_and_missing_paths() {
        let tmp = tempfile::tempdir().unwrap();
        let plain = tmp.path().join("plain/dir");
        std::fs::create_dir_all(&plain).unwrap();
        assert_eq!(resolve(plain.to_str().unwrap()), canon(&plain));
        let gone = tmp.path().join("no/such/dir");
        assert_eq!(resolve(gone.to_str().unwrap()), gone.to_str().unwrap());
        assert_eq!(project_name("/home/demo/alpha"), "alpha");
        assert_eq!(project_name("/"), "/");
    }

    #[test]
    fn deleted_worktree_folds_into_its_repository() {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("repo");
        git_init(&repo);
        let repo_s = canon(&repo);
        for tail in [
            ".claude/worktrees/explore",
            ".claude/worktrees/explore/src/deep",
            ".git/worktrees/explore",
        ] {
            let gone = repo.join(tail);
            assert!(!gone.exists());
            assert_eq!(resolve(gone.to_str().unwrap()), repo_s, "{tail}");
        }
        // The repository itself is gone too: fall back to its path as recorded.
        let lost = "/no/such/repo/.claude/worktrees/x";
        assert_eq!(resolve(lost), "/no/such/repo");
        // A marker at the very start leaves nothing to fold into.
        assert_eq!(resolve("/.claude/worktrees/x"), "/.claude/worktrees/x");
        // Ordinary missing paths are untouched.
        assert_eq!(resolve("/no/such/worktrees/x"), "/no/such/worktrees/x");
        assert_eq!(enclosing_repo("/a/b/.claude/worktrees/c"), Some("/a/b"));
    }

    #[test]
    fn enrich_refolds_sessions_filed_under_a_deleted_worktree() {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("repo");
        git_init(&repo);
        let gone = format!("{}/.claude/worktrees/old", repo.display());
        let mut store = crate::store::Store::open_in_memory().unwrap();
        let conn = &mut store.conn;
        conn.execute(
            "INSERT INTO sessions (tool, session_id, cwd, project, project_name)
             VALUES ('codex', 's1', ?1, ?1, 'old')",
            params![gone],
        )
        .unwrap();
        enrich(conn, &[]).unwrap();
        let (p, n): (String, String) = conn
            .query_row("SELECT project, project_name FROM sessions", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .unwrap();
        assert_eq!(p, canon(&repo));
        assert_eq!(n, "repo");
    }

    fn seed(conn: &Connection, rows: &[(&str, &str, Option<&str>)]) {
        for (tool, sid, cwd) in rows {
            conn.execute(
                "INSERT INTO sessions (tool, session_id, cwd, started_at_ms, ended_at_ms)
                 VALUES (?1, ?2, ?3, 1000, 2000)",
                params![tool, sid, cwd],
            )
            .unwrap();
        }
    }

    #[test]
    fn enrich_sets_projects_and_counts_missing_cwd() {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("beta");
        git_init(&repo);
        std::fs::create_dir_all(repo.join("sub")).unwrap();
        let mut store = crate::store::Store::open_in_memory().unwrap();
        let sub = repo.join("sub");
        seed(
            &store.conn,
            &[
                ("claude-code", "c1", Some(repo.to_str().unwrap())),
                ("claude-code", "c2", Some(sub.to_str().unwrap())),
                ("codex", "x1", Some("/no/such/place")),
                ("antigravity", "a1", None),
            ],
        );
        let r = enrich(&mut store.conn, &[tmp.path().join("missing.sqlite")]).unwrap();
        assert_eq!(r.projects_set, 3);
        assert_eq!(r.without_cwd, 1);
        assert_eq!(r.t3.databases_read, 0);
        let got: Vec<(String, Option<String>, Option<String>)> = store
            .conn
            .prepare("SELECT session_id, project, project_name FROM sessions ORDER BY session_id")
            .unwrap()
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let beta = canon(&repo);
        assert_eq!(got[0], ("a1".into(), None, None));
        assert_eq!(
            got[1],
            ("c1".into(), Some(beta.clone()), Some("beta".into()))
        );
        assert_eq!(got[2], ("c2".into(), Some(beta), Some("beta".into())));
        assert_eq!(
            got[3],
            (
                "x1".into(),
                Some("/no/such/place".into()),
                Some("place".into())
            )
        );
        // A second run has nothing left to do.
        let r = enrich(&mut store.conn, &[]).unwrap();
        assert_eq!(r.projects_set, 0);
        assert_eq!(r.without_cwd, 1);
    }

    #[test]
    fn lookup_by_path_name_and_ambiguity() {
        let store = crate::store::Store::open_in_memory().unwrap();
        for (sid, p) in [
            ("s1", "/w/one/app"),
            ("s2", "/w/two/app"),
            ("s3", "/w/Tool"),
            ("s4", "/w/Tool"),
        ] {
            store
                .conn
                .execute(
                    "INSERT INTO sessions (tool, session_id, project, project_name)
                     VALUES ('codex', ?1, ?2, ?3)",
                    params![sid, p, project_name(p)],
                )
                .unwrap();
        }
        let l = |k| lookup(&store.conn, k).unwrap();
        assert_eq!(l("/w/one/app"), Lookup::Found("/w/one/app".into()));
        assert_eq!(l("/w/one/app/"), Lookup::Found("/w/one/app".into()));
        assert_eq!(l("tool"), Lookup::Found("/w/Tool".into()));
        assert_eq!(l("nothing"), Lookup::NotFound);
        match l("APP") {
            Lookup::Ambiguous(c) => {
                assert_eq!(c.len(), 2);
                assert_eq!(c[0].project, "/w/one/app");
            }
            other => panic!("{other:?}"),
        }
    }
}
