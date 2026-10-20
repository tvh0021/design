//! Task-kind labels (ticket 06).
//!
//! Each top-level session gets one label at ingest, chosen by keyword rules over
//! its opening user prompt. Only the label is stored; the prompt never is.
//! A hand-set label lives in `session_labels` (so it survives rescans and a
//! rebuild of the raw-log rows) and wins over `sessions.label` at query time.
//! Subagent sessions have no label of their own: queries use their root's.

use crate::query::{Tokens, rfc3339};
use anyhow::{Result, bail};
use rusqlite::{Connection, Transaction, params};
use serde::Serialize;

pub const KINDS: &[&str] = &[
    "research",
    "coding",
    "debugging",
    "writing",
    "review",
    "data-analysis",
    "ops-config",
    "other",
];

/// Shown for sessions with no label (logs ingested before labels existed, and
/// subagent sessions whose parent is unknown).
pub const UNLABELLED: &str = "unlabelled";

/// The rules read the opening line (up to this many characters) first, then the
/// start of the prompt (up to the window size). Nothing beyond that is read.
const FIRST_LINE_CHARS: usize = 300;
const PROMPT_WINDOW_CHARS: usize = 600;

/// One classification rule. A prompt matches when any `stems` entry starts at a
/// word boundary (so `"fail"` matches `failing`) or any `words` entry appears as
/// a whole word. Entries are lowercase.
pub struct Rule {
    pub label: &'static str,
    pub stems: &'static [&'static str],
    pub words: &'static [&'static str],
}

/// Ordered: the first rule that matches decides. Specific intents come before
/// the broad `coding` rule; a prompt that matches nothing is `other`.
pub const RULES: &[Rule] = &[
    Rule {
        label: "debugging",
        stems: &[
            "debug",
            "bug",
            "traceback",
            "stack trace",
            "stacktrace",
            "exception",
            "crash",
            "error",
            "fail",
            "broken",
            "not working",
            "doesn't work",
            "doesnt work",
            "isn't working",
            "troubleshoot",
            "segfault",
            "diagnos",
            "freez",
        ],
        words: &[
            "fix", "fixes", "fixed", "fixing", "hang", "hangs", "hanging", "stuck", "panic", "oom",
            "npe",
        ],
    },
    Rule {
        label: "review",
        stems: &[
            "review",
            "pull request",
            "audit",
            "critique",
            "sanity check",
            "double-check",
            "double check",
            "second opinion",
            "look over",
            "feedback on",
        ],
        words: &["pr", "prs", "lgtm"],
    },
    Rule {
        label: "ops-config",
        stems: &[
            "install",
            "configur",
            "config",
            "set up",
            "setup",
            "deploy",
            "docker",
            "kubernetes",
            "ssh",
            "cron",
            "launchd",
            "systemd",
            "homebrew",
            "permission",
            "credential",
            "environment variable",
            "dotfile",
            "slurm",
            "sbatch",
            "nginx",
            "mcp",
            "hook",
            "plugin",
            "settings.json",
            "upgrade",
            "git remote",
            "github action",
            "worktree",
            "symlink",
        ],
        words: &[
            "ci", "env", "pip", "npm", "apt", "brew", "venv", "conda", "uv", "vpn", "dns", "sudo",
            "chmod",
        ],
    },
    Rule {
        label: "data-analysis",
        stems: &[
            "analy",
            "dataset",
            "data set",
            "dataframe",
            "pandas",
            "numpy",
            "matplotlib",
            "plot",
            "histogram",
            "statistic",
            "correlation",
            "simulation",
            "spectr",
            "hdf5",
            "parquet",
            "netcdf",
            "jupyter",
            "notebook",
            "visualiz",
            "chart",
            "distribution",
            "calculat",
            "benchmark",
        ],
        words: &["csv", "tsv", "fits", "dataframes"],
    },
    Rule {
        label: "writing",
        stems: &[
            "draft",
            "rewrite",
            "rephrase",
            "proofread",
            "edit this",
            "email",
            "cover letter",
            "resume",
            "r\u{e9}sum\u{e9}",
            "abstract",
            "manuscript",
            "proposal",
            "essay",
            "blog",
            "summariz",
            "summaris",
            "polish",
            "wording",
            "paragraph",
            "write up",
            "write an email",
            "write a letter",
            "write a summary",
            "write the intro",
            "tailor",
            "readme",
        ],
        words: &["paper", "letter", "docs", "documentation", "slides"],
    },
    Rule {
        label: "research",
        stems: &[
            "research",
            "investigate",
            "look up",
            "look into",
            "find out",
            "find me",
            "search for",
            "compare",
            "comparison",
            "what is",
            "what are",
            "how does",
            "how do",
            "how can",
            "explain",
            "survey",
            "literature",
            "best practice",
            "options for",
            "recommend",
            "pros and cons",
            "difference between",
            "alternatives",
            "arxiv",
            "citation",
            "apartment",
            "listing",
            "rental",
        ],
        words: &["why", "sources"],
    },
    Rule {
        label: "coding",
        stems: &[
            "implement",
            "refactor",
            "function",
            "script",
            "feature",
            "endpoint",
            "module",
            "unit test",
            "compile",
            "commit",
            "migration",
            "schema",
            "parser",
            "rename",
            "cargo",
            "python",
            "rust",
            "typescript",
            "javascript",
            "react",
            "ticket",
            "code",
        ],
        words: &[
            "add", "create", "build", "make", "change", "update", "remove", "delete", "move",
            "test", "tests", "api", "cli", "class", "method", "repo", "branch", "merge", "wire",
        ],
    },
];

/// Task kind for a prompt: the label of the first rule that matches the opening
/// line, else the first rule that matches the opening `PROMPT_WINDOW_CHARS`
/// characters, else `other`. The opening line gets priority because pasted
/// context further down mentions errors and reviews that are not the task.
pub fn classify(prompt: &str) -> &'static str {
    let lower = |n: usize, text: &str| -> String {
        text.chars().take(n).flat_map(char::to_lowercase).collect()
    };
    let first_line = prompt
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .unwrap_or("");
    let first = lower(FIRST_LINE_CHARS, first_line);
    let window = lower(PROMPT_WINDOW_CHARS, prompt);
    [first, window]
        .iter()
        .find_map(|text| RULES.iter().find(|r| matches_rule(text, r)))
        .map_or("other", |r| r.label)
}

fn matches_rule(text: &str, rule: &Rule) -> bool {
    rule.stems.iter().any(|s| find_at_boundary(text, s, false))
        || rule.words.iter().any(|w| find_at_boundary(text, w, true))
}

/// True when `needle` occurs in `text` starting at a word boundary (and, with
/// `whole`, also ending at one).
fn find_at_boundary(text: &str, needle: &str, whole: bool) -> bool {
    let is_word = |c: char| c.is_alphanumeric();
    let mut from = 0;
    while let Some(i) = text[from..].find(needle) {
        let start = from + i;
        let end = start + needle.len();
        let before_ok = text[..start]
            .chars()
            .next_back()
            .is_none_or(|c| !is_word(c));
        let after_ok = !whole || text[end..].chars().next().is_none_or(|c| !is_word(c));
        if before_ok && after_ok {
            return true;
        }
        from = start + text[start..].chars().next().map_or(1, char::len_utf8);
    }
    false
}

/// `kind` as a canonical label, if it is one.
pub fn parse_kind(kind: &str) -> Option<&'static str> {
    let k = kind.trim().to_lowercase();
    KINDS.iter().copied().find(|c| *c == k)
}

/// Store the rule label for a session that has none yet. Never touches
/// `session_labels`, so a hand-set label is unaffected.
pub fn set_rule_label(tx: &Transaction, tool: &str, session_id: &str, label: &str) -> Result<()> {
    tx.execute(
        "UPDATE sessions SET label = ?3, label_source = 'rule'
         WHERE tool = ?1 AND session_id = ?2 AND label IS NULL",
        params![tool, session_id, label],
    )?;
    Ok(())
}

/// Find one top-level session by exact id or unique id prefix.
pub fn resolve_session(conn: &Connection, id_or_prefix: &str) -> Result<(String, String)> {
    if id_or_prefix.is_empty() {
        bail!("session id is empty");
    }
    let escaped = id_or_prefix
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_");
    let mut stmt = conn.prepare(
        "SELECT tool, session_id, is_subagent FROM sessions
         WHERE session_id = ?1 OR session_id LIKE ?2 ESCAPE '\\'
         ORDER BY (session_id = ?1) DESC, tool, session_id LIMIT 6",
    )?;
    let rows = stmt
        .query_map(params![id_or_prefix, format!("{escaped}%")], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, i64>(2)? != 0,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    // An exact match wins even when it is also a prefix of longer ids.
    let exact: Vec<_> = rows.iter().filter(|r| r.1 == id_or_prefix).collect();
    let pick = if exact.len() == 1 {
        exact
    } else {
        rows.iter().collect()
    };
    match pick.as_slice() {
        [] => bail!("no session matches {id_or_prefix:?}"),
        [(tool, id, true)] => bail!(
            "{tool} session {id} is a subagent; it inherits its parent's label. Label the parent instead"
        ),
        [(tool, id, false)] => Ok((tool.clone(), id.clone())),
        many => bail!(
            "{id_or_prefix:?} is ambiguous: {}",
            many.iter()
                .map(|(t, i, _)| format!("{t}:{i}"))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// Record a hand-set label. Returns the label it replaced, if any.
pub fn set_manual_label(
    conn: &Connection,
    tool: &str,
    session_id: &str,
    kind: &str,
    now_ms: i64,
) -> Result<Option<String>> {
    let Some(kind) = parse_kind(kind) else {
        bail!(
            "unknown label {kind:?}; choose one of: {}",
            KINDS.join(", ")
        );
    };
    let before = effective_label(conn, tool, session_id)?;
    conn.execute(
        "INSERT INTO session_labels (tool, session_id, label, set_at) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(tool, session_id) DO UPDATE SET label = excluded.label, set_at = excluded.set_at",
        params![tool, session_id, kind, now_ms],
    )?;
    Ok(before)
}

/// Label of one top-level session: hand-set if present, else the rule result.
pub fn effective_label(conn: &Connection, tool: &str, session_id: &str) -> Result<Option<String>> {
    Ok(conn.query_row(
        "SELECT COALESCE(
             (SELECT label FROM session_labels WHERE tool = ?1 AND session_id = ?2),
             (SELECT label FROM sessions WHERE tool = ?1 AND session_id = ?2))",
        params![tool, session_id],
        |r| r.get(0),
    )?)
}

/// CTE `eff(tool, session_id, is_subagent, label, manual)`: every session with
/// the label it is reported under. A subagent takes its root session's label,
/// following `parent_session_id` through nested subagents. Subagents whose
/// parent is unknown get a NULL label.
const EFFECTIVE_LABELS: &str = r#"
    RECURSIVE chain(tool, session_id, root_id, depth) AS (
        SELECT tool, session_id, session_id, 0 FROM sessions WHERE parent_session_id IS NULL
        UNION
        SELECT s.tool, s.session_id, c.root_id, c.depth + 1
        FROM chain c JOIN sessions s
          ON s.tool = c.tool AND s.parent_session_id = c.session_id
        WHERE c.depth < 16
    ),
    eff(tool, session_id, is_subagent, label, manual) AS (
        SELECT c.tool, c.session_id, s.is_subagent,
               COALESCE(m.label, CASE WHEN r.is_subagent = 0 THEN r.label END),
               m.label IS NOT NULL
        FROM chain c
        JOIN sessions s ON s.tool = c.tool AND s.session_id = c.session_id
        JOIN sessions r ON r.tool = c.tool AND r.session_id = c.root_id
        LEFT JOIN session_labels m ON m.tool = r.tool AND m.session_id = r.session_id
    )
"#;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct LabelRow {
    pub label: String,
    /// Top-level sessions with this label.
    pub sessions: i64,
    /// Of those, how many were labelled by hand.
    pub manual_sessions: i64,
    /// Subagent sessions that inherit this label.
    pub subagent_sessions: i64,
    #[serde(flatten)]
    pub tokens: Tokens,
}

#[derive(Debug, Clone, Serialize)]
pub struct LabelReport {
    pub generated_at: String,
    pub totals: Tokens,
    pub labels: Vec<LabelRow>,
}

/// Session and request totals per label (all time).
pub fn label_report(conn: &Connection, now_ms: i64) -> Result<LabelReport> {
    let sql = format!(
        r#"WITH {EFFECTIVE_LABELS},
        req(tool, session_id, n, inp, cr, cc, outp, cost, unpriced) AS (
            SELECT tool, session_id, COUNT(*), SUM(input_tokens), SUM(cache_read_input_tokens),
                   SUM(cache_creation_input_tokens), SUM(output_tokens), SUM(cost_usd),
                   COUNT(*) - COUNT(cost_usd)
            FROM requests GROUP BY tool, session_id
        )
        SELECT COALESCE(e.label, '{UNLABELLED}'),
               SUM(e.is_subagent = 0), SUM(e.is_subagent = 0 AND e.manual),
               SUM(e.is_subagent = 1),
               COALESCE(SUM(q.n), 0), COALESCE(SUM(q.inp), 0), COALESCE(SUM(q.cr), 0),
               COALESCE(SUM(q.cc), 0), COALESCE(SUM(q.outp), 0), SUM(q.cost),
               COALESCE(SUM(q.unpriced), 0)
        FROM eff e LEFT JOIN req q ON q.tool = e.tool AND q.session_id = e.session_id
        GROUP BY 1
        ORDER BY 5 DESC, 1"#
    );
    let mut stmt = conn.prepare(&sql)?;
    let labels = stmt
        .query_map([], |r| {
            Ok(LabelRow {
                label: r.get(0)?,
                sessions: r.get(1)?,
                manual_sessions: r.get(2)?,
                subagent_sessions: r.get(3)?,
                tokens: Tokens {
                    requests: r.get(4)?,
                    input_tokens: r.get(5)?,
                    cache_read_input_tokens: r.get(6)?,
                    cache_creation_input_tokens: r.get(7)?,
                    output_tokens: r.get(8)?,
                    estimated_cost_usd: r.get(9)?,
                    unpriced_requests: r.get(10)?,
                },
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut totals = Tokens::default();
    for l in &labels {
        add_tokens(&mut totals, &l.tokens);
    }
    Ok(LabelReport {
        generated_at: rfc3339(now_ms),
        totals,
        labels,
    })
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

pub fn render_label_table(r: &LabelReport) -> String {
    let mut rows: Vec<Vec<String>> = vec![
        [
            "label",
            "sessions",
            "manual",
            "subagents",
            "requests",
            "input",
            "output",
            "est. cost",
        ]
        .map(String::from)
        .to_vec(),
    ];
    for l in &r.labels {
        rows.push(vec![
            l.label.clone(),
            l.sessions.to_string(),
            l.manual_sessions.to_string(),
            l.subagent_sessions.to_string(),
            l.tokens.requests.to_string(),
            l.tokens.input_tokens.to_string(),
            l.tokens.output_tokens.to_string(),
            cost_cell(&l.tokens),
        ]);
    }
    render_rows(&rows, 1)
}

fn cost_cell(t: &Tokens) -> String {
    match t.estimated_cost_usd {
        Some(c) if t.unpriced_requests > 0 => format!("${c:.2}*"),
        Some(c) => format!("${c:.2}"),
        None => "-".into(),
    }
}

/// Left-align the first `left` columns, right-align the rest.
fn render_rows(rows: &[Vec<String>], left: usize) -> String {
    let cols = rows.first().map_or(0, Vec::len);
    let widths: Vec<usize> = (0..cols)
        .map(|i| rows.iter().map(|r| r[i].chars().count()).max().unwrap_or(0))
        .collect();
    let mut out = String::new();
    for r in rows {
        let mut line = String::new();
        for (i, cell) in r.iter().enumerate() {
            if i < left {
                line.push_str(&format!("{:<w$}  ", cell, w = widths[i]));
            } else {
                line.push_str(&format!("{:>w$}  ", cell, w = widths[i]));
            }
        }
        out.push_str(line.trim_end());
        out.push('\n');
    }
    out
}

/// One `query summary --group-by label` row: a label within a tool.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct LabelSummaryGroup {
    pub label: String,
    pub tool: String,
    #[serde(flatten)]
    pub tokens: Tokens,
}

#[derive(Debug, Clone, Serialize)]
pub struct LabelSummary {
    pub generated_at: String,
    pub since: Option<String>,
    pub rates_fetched_at: Option<String>,
    pub group_by: &'static str,
    pub totals: Tokens,
    pub groups: Vec<LabelSummaryGroup>,
}

/// Request totals grouped by (label, tool) for requests at or after `since_ms`.
pub fn summary_by_label(
    conn: &Connection,
    since_ms: Option<i64>,
    now_ms: i64,
) -> Result<LabelSummary> {
    let sql = format!(
        r#"WITH {EFFECTIVE_LABELS}
        SELECT COALESCE(e.label, '{UNLABELLED}'), q.tool, COUNT(*),
               SUM(q.input_tokens), SUM(q.cache_read_input_tokens),
               SUM(q.cache_creation_input_tokens), SUM(q.output_tokens),
               SUM(q.cost_usd), COUNT(*) - COUNT(q.cost_usd)
        FROM requests q
        LEFT JOIN eff e ON e.tool = q.tool AND e.session_id = q.session_id
        WHERE q.ts_ms >= ?1
        GROUP BY 1, 2
        ORDER BY 1, SUM(q.input_tokens + q.cache_read_input_tokens
                        + q.cache_creation_input_tokens + q.output_tokens) DESC, 2"#
    );
    let mut stmt = conn.prepare(&sql)?;
    let groups = stmt
        .query_map(params![since_ms.unwrap_or(i64::MIN)], |r| {
            Ok(LabelSummaryGroup {
                label: r.get(0)?,
                tool: r.get(1)?,
                tokens: Tokens {
                    requests: r.get(2)?,
                    input_tokens: r.get(3)?,
                    cache_read_input_tokens: r.get(4)?,
                    cache_creation_input_tokens: r.get(5)?,
                    output_tokens: r.get(6)?,
                    estimated_cost_usd: r.get(7)?,
                    unpriced_requests: r.get(8)?,
                },
            })
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut totals = Tokens::default();
    for g in &groups {
        add_tokens(&mut totals, &g.tokens);
    }
    Ok(LabelSummary {
        generated_at: rfc3339(now_ms),
        since: since_ms.map(rfc3339),
        rates_fetched_at: None,
        group_by: "label",
        totals,
        groups,
    })
}

pub fn render_label_summary_table(s: &LabelSummary) -> String {
    let mut rows: Vec<Vec<String>> = vec![
        [
            "label",
            "tool",
            "requests",
            "input",
            "cache_read",
            "cache_create",
            "output",
            "est. cost",
        ]
        .map(String::from)
        .to_vec(),
    ];
    let line = |label: &str, tool: &str, t: &Tokens| {
        vec![
            label.to_string(),
            tool.to_string(),
            t.requests.to_string(),
            t.input_tokens.to_string(),
            t.cache_read_input_tokens.to_string(),
            t.cache_creation_input_tokens.to_string(),
            t.output_tokens.to_string(),
            cost_cell(t),
        ]
    };
    for g in &s.groups {
        rows.push(line(&g.label, &g.tool, &g.tokens));
    }
    rows.push(line("TOTAL", "", &s.totals));
    let mut out = format!("since: {}\n", s.since.as_deref().unwrap_or("all time"));
    out.push_str(&render_rows(&rows, 2));
    out.push_str("labels: a subagent's requests count under its parent session's label\n");
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    #[test]
    fn each_kind_has_a_matching_prompt() {
        let cases = [
            (
                "The build is failing with a stack trace, please fix it",
                "debugging",
            ),
            ("Review this pull request for correctness", "review"),
            ("Install docker and configure the launchd job", "ops-config"),
            (
                "Plot a histogram of the halo mass distribution from the CSV",
                "data-analysis",
            ),
            ("Draft an email to my advisor about the proposal", "writing"),
            (
                "Research the options for hosting and compare their prices",
                "research",
            ),
            ("Implement a parser function for the new schema", "coding"),
            ("hello there", "other"),
            ("", "other"),
        ];
        for (prompt, want) in cases {
            assert_eq!(classify(prompt), want, "{prompt}");
        }
    }

    #[test]
    fn rules_are_ordered_and_first_match_wins() {
        // Both debugging ("error") and coding ("implement") match; debugging is listed first.
        assert_eq!(classify("implement a handler for this error"), "debugging");
        // Both review and ops-config ("config") match; review is first.
        assert_eq!(classify("review the config"), "review");
        // `other` is never a rule label and the labels in RULES are all valid kinds.
        assert!(RULES.iter().all(|r| parse_kind(r.label).is_some()));
        assert!(RULES.iter().all(|r| r.label != "other"));
        assert_eq!(
            RULES
                .iter()
                .map(|r| r.label)
                .collect::<std::collections::BTreeSet<_>>()
                .len(),
            KINDS.len() - 1
        );
    }

    #[test]
    fn matching_respects_word_boundaries() {
        // "fixture" and "prefix" must not match the whole words fix / pr.
        assert_eq!(classify("a fixture for the prefix"), "other");
        // Stems match inflections at the word start.
        assert_eq!(classify("it keeps failing"), "debugging");
        assert_eq!(classify("it was reviewed"), "review");
        // A stem does not match in the middle of a word.
        assert_eq!(classify("unreviewable"), "other");
        // Case and punctuation do not matter.
        assert_eq!(classify("FIX: the thing."), "debugging");
        // Non-ASCII text does not panic or misalign.
        assert_eq!(classify("r\u{e9}sum\u{e9} \u{2014} polish it"), "writing");
        assert_eq!(classify("\u{1f600}\u{1f600} fix \u{1f600}"), "debugging");
    }

    #[test]
    fn only_the_start_of_a_long_prompt_is_read() {
        let mut p = "hello ".repeat(200);
        p.push_str("traceback");
        assert_eq!(classify(&p), "other");
        assert_eq!(classify(&format!("traceback {p}")), "debugging");
    }

    #[test]
    fn the_opening_line_outranks_later_lines() {
        // "error" below would make this debugging if the whole window ranked equally.
        assert_eq!(
            classify("Draft the abstract\nthe log shows an error and a crash"),
            "writing"
        );
        // With nothing on the first line, the rest of the window decides.
        assert_eq!(classify("hello\nthe log shows an error"), "debugging");
    }

    #[test]
    fn kinds_parse_case_insensitively() {
        assert_eq!(parse_kind(" Data-Analysis "), Some("data-analysis"));
        assert_eq!(parse_kind("nonsense"), None);
    }

    fn seeded() -> Store {
        let store = Store::open_in_memory().unwrap();
        for (id, sub, parent, label) in [
            ("aaaa1111", 0, None, Some("coding")),
            ("aaaa2222", 0, None, Some("research")),
            ("sub-1", 1, Some("aaaa1111"), None),
            ("sub-1-1", 1, Some("sub-1"), None),
            ("orphan", 1, None, None),
        ] {
            store
                .conn
                .execute(
                    "INSERT INTO sessions (tool, session_id, is_subagent, parent_session_id, label)
                     VALUES ('claude-code', ?1, ?2, ?3, ?4)",
                    params![id, sub, parent, label],
                )
                .unwrap();
        }
        store
    }

    #[test]
    fn resolve_by_exact_id_or_unique_prefix() {
        let s = seeded();
        let ok = |p: &str| resolve_session(&s.conn, p).map(|(_, id)| id);
        assert_eq!(ok("aaaa1111").unwrap(), "aaaa1111");
        assert_eq!(ok("aaaa2").unwrap(), "aaaa2222");
        assert!(ok("aaaa").unwrap_err().to_string().contains("ambiguous"));
        assert!(ok("zzz").unwrap_err().to_string().contains("no session"));
        assert!(ok("sub-1-").unwrap_err().to_string().contains("subagent"));
        // "sub-1" is exact even though "sub-1-1" also starts with it, and it is a subagent.
        assert!(ok("sub-1").unwrap_err().to_string().contains("subagent"));
        assert!(ok("").is_err());
        assert!(ok("100%").is_err(), "LIKE wildcards are escaped");
    }

    #[test]
    fn manual_label_wins_and_subagents_inherit() {
        let s = seeded();
        assert_eq!(
            effective_label(&s.conn, "claude-code", "aaaa1111")
                .unwrap()
                .as_deref(),
            Some("coding")
        );
        let before = set_manual_label(&s.conn, "claude-code", "aaaa1111", "Writing", 5).unwrap();
        assert_eq!(before.as_deref(), Some("coding"));
        assert_eq!(
            effective_label(&s.conn, "claude-code", "aaaa1111")
                .unwrap()
                .as_deref(),
            Some("writing")
        );
        assert!(set_manual_label(&s.conn, "claude-code", "aaaa1111", "bogus", 6).is_err());
        let r = label_report(&s.conn, 0).unwrap();
        let row = |l: &str| r.labels.iter().find(|x| x.label == l).unwrap();
        assert_eq!(row("writing").sessions, 1);
        assert_eq!(row("writing").manual_sessions, 1);
        // sub-1 and its nested child inherit the parent's (manual) label.
        assert_eq!(row("writing").subagent_sessions, 2);
        assert_eq!(row("research").sessions, 1);
        // The orphan subagent has no root label.
        assert_eq!(row(UNLABELLED).subagent_sessions, 1);
        assert!(r.labels.iter().all(|l| l.label != "coding"));
    }

    #[test]
    fn rule_label_is_set_once() {
        let mut s = Store::open_in_memory().unwrap();
        s.conn
            .execute(
                "INSERT INTO sessions (tool, session_id) VALUES ('codex', 'x')",
                [],
            )
            .unwrap();
        let tx = s.conn.transaction().unwrap();
        set_rule_label(&tx, "codex", "x", "coding").unwrap();
        set_rule_label(&tx, "codex", "x", "writing").unwrap();
        tx.commit().unwrap();
        let (l, src): (String, String) = s
            .conn
            .query_row("SELECT label, label_source FROM sessions", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .unwrap();
        assert_eq!((l.as_str(), src.as_str()), ("coding", "rule"));
    }
}
