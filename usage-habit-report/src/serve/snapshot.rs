//! Static public exports use the private dashboard's queries and templates.
//! Only the page model is redacted, before any HTML is rendered.

use super::{data::Window, html, routes};
use anyhow::{Context, Result};
use chrono::Local;
use rusqlite::Connection;
use std::{collections::BTreeMap, path::Path};

/// Relative filenames work both on Pages and under file://.
pub fn filename(window: Window, tool: Option<&str>, single_window: bool) -> String {
    let window = if single_window || window == Window::FourWeeks {
        String::new()
    } else {
        format!("-{}", window.key())
    };
    let tool = tool.map(|t| format!("-{t}")).unwrap_or_default();
    format!("index{window}{tool}.html")
}

/// One mapping for the whole run, including projects outside the top ten.
/// All-time estimated cost descending, then path for deterministic ties.
struct Redaction {
    projects: BTreeMap<String, String>,
}

fn letters(mut index: usize) -> String {
    let mut chars = Vec::new();
    loop {
        chars.push((b'A' + (index % 26) as u8) as char);
        if index < 26 {
            break;
        }
        index = index / 26 - 1;
    }
    chars.into_iter().rev().collect()
}

fn round(cost: &mut Option<f64>) {
    *cost = cost.map(f64::round);
}

impl Redaction {
    fn load(conn: &Connection, now_ms: i64) -> Result<Self> {
        let mut projects = crate::project::list(conn, None, now_ms)?.projects;
        projects.sort_by(|a, b| {
            b.tokens
                .estimated_cost_usd
                .unwrap_or(0.0)
                .total_cmp(&a.tokens.estimated_cost_usd.unwrap_or(0.0))
                .then(a.project.cmp(&b.project))
        });
        Ok(Self {
            projects: projects
                .into_iter()
                .filter_map(|p| p.project)
                .enumerate()
                .map(|(i, path)| (path, format!("project {}", letters(i))))
                .collect(),
        })
    }

    fn project(&self, path: &mut Option<String>, name: &mut Option<String>) {
        *name = path.as_ref().map(|p| {
            // Fail closed if a future query supplies a project absent from the mapping.
            self.projects
                .get(p)
                .cloned()
                .unwrap_or_else(|| "project unknown".into())
        });
        *path = None;
    }

    fn apply(&self, p: &mut html::Page, windows: &[Window]) {
        p.audience = html::Audience::Public {
            windows: windows.to_vec(),
        };
        // These rows have titles and ids but no project field. Keep the panel's
        // aggregate figures and replace this sub-list with an explanatory note.
        p.agents.heaviest_fanouts.clear();
        for line in p
            .sessions
            .longest
            .iter_mut()
            .chain(&mut p.sessions.costliest)
        {
            line.session_id.clear();
            line.title = None;
            self.project(&mut line.project, &mut line.project_name);
            round(&mut line.cost_usd);
        }
        for line in &mut p.projects.lines {
            self.project(&mut line.row.project, &mut line.row.name);
            round(&mut line.row.tokens.estimated_cost_usd);
            for (_, _, cost) in &mut line.tools {
                round(cost);
            }
        }
        round(&mut p.totals.total.estimated_cost_usd);
        for row in &mut p.totals.by_tool {
            round(&mut row.tokens.estimated_cost_usd);
        }
        for cell in p.heatmap.cells.iter_mut().flatten() {
            round(&mut cell.cost_usd);
        }
        for cell in p.spend.periods.iter_mut().flat_map(|p| &mut p.cells) {
            round(&mut cell.0);
        }
        for cell in p
            .spend_totals
            .rows
            .iter_mut()
            .chain(&mut p.spend_totals.columns)
            .chain(std::iter::once(&mut p.spend_totals.all))
        {
            round(&mut cell.0);
        }
    }
}

/// Write all windows by default; an explicit window is written as index.html.
/// Each window includes all-tools and the three tool-filtered variants.
/// One read transaction keeps the pseudonyms and every page consistent while
/// another process scans the store. No migrations, pricing, or network access.
pub fn write(db: &Path, out: &Path, window: Option<Window>, now_ms: i64) -> Result<usize> {
    let conn =
        super::open_read_only(db)?.context("no usage store; run usage-habit-report scan first")?;
    conn.execute_batch("BEGIN DEFERRED")?;
    let redaction = Redaction::load(&conn, now_ms)?;
    let windows = window
        .map(|w| vec![w])
        .unwrap_or_else(|| Window::ALL.to_vec());
    // Load and redact every page before writing, so query failures leave no partial export.
    let mut pages = Vec::new();
    for &w in &windows {
        for tool in std::iter::once(None).chain(super::data::TOOLS.into_iter().map(Some)) {
            let mut page = routes::load_page(
                &conn,
                routes::Params {
                    window: Some(w),
                    tool,
                },
                now_ms,
                &Local,
                None,
            )?;
            redaction.apply(&mut page, &windows);
            pages.push((filename(w, tool, windows.len() == 1), html::render(&page)));
        }
    }
    conn.execute_batch("COMMIT")?;
    std::fs::create_dir_all(out)?;
    for (name, body) in &pages {
        std::fs::write(out.join(name), body)?;
    }
    Ok(pages.len())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::serve::data::tests::{NOW, panel_fixture};

    fn page(store: &crate::store::Store, window: Window) -> html::Page {
        routes::load_page(
            &store.conn,
            routes::Params {
                window: Some(window),
                tool: None,
            },
            NOW,
            &Local,
            None,
        )
        .unwrap()
    }

    #[test]
    fn project_pseudonyms_are_stable_across_windows_and_remove_paths() {
        let store = panel_fixture();
        let redaction = Redaction::load(&store.conn, NOW).unwrap();
        assert_eq!(
            redaction.projects,
            Redaction::load(&store.conn, NOW).unwrap().projects
        );
        for window in Window::ALL {
            let mut p = page(&store, window);
            let expected: Vec<_> = p
                .projects
                .lines
                .iter()
                .map(|l| {
                    l.row
                        .project
                        .as_ref()
                        .map(|path| redaction.projects[path].clone())
                })
                .collect();
            redaction.apply(&mut p, &Window::ALL);
            for (line, name) in p.projects.lines.iter().zip(expected) {
                assert!(line.row.project.is_none());
                assert_eq!(line.row.name, name);
            }
            for s in p.sessions.longest.iter().chain(&p.sessions.costliest) {
                assert!(s.project.is_none());
                assert!(
                    s.project_name
                        .as_ref()
                        .is_none_or(|n| n.starts_with("project "))
                );
            }
            let html = html::render(&p);
            assert!(!html.contains("title=\"/"));
            assert!(!html.contains("/home/demo/"));
        }
        assert_eq!(
            [0, 25, 26, 27, 701, 702].map(letters),
            ["A", "Z", "AA", "AB", "ZZ", "AAA"]
        );
    }

    #[test]
    fn session_titles_and_ids_are_removed_before_rendering() {
        let store = panel_fixture();
        let mut p = page(&store, Window::All);
        assert!(!p.agents.heaviest_fanouts.is_empty());
        Redaction::load(&store.conn, NOW)
            .unwrap()
            .apply(&mut p, &Window::ALL);
        assert!(p.agents.heaviest_fanouts.is_empty());
        for s in p.sessions.longest.iter().chain(&p.sessions.costliest) {
            assert!(s.session_id.is_empty());
            assert!(s.title.is_none());
        }
        let html = html::render(&p);
        assert!(html.contains("Heaviest fan-outs omitted"));
        assert!(!html.contains("Demo task"));
        assert!(!html.contains("untitled session"));
    }

    #[test]
    fn all_cost_fields_are_rounded_independently_from_exact_totals() {
        let store = panel_fixture();
        let mut p = page(&store, Window::All);
        let exact_total = p.totals.total.estimated_cost_usd.unwrap();
        let exact_spend = p.spend_totals.clone();
        let exact_heatmap = p.heatmap.clone();
        Redaction::load(&store.conn, NOW)
            .unwrap()
            .apply(&mut p, &Window::ALL);
        assert_eq!(p.totals.total.estimated_cost_usd, Some(exact_total.round()));
        assert_eq!(p.spend_totals.all.0, exact_spend.all.0.map(f64::round));
        for (a, b) in p
            .heatmap
            .cells
            .iter()
            .flatten()
            .zip(exact_heatmap.cells.iter().flatten())
        {
            assert_eq!(a.cost_usd, b.cost_usd.map(f64::round));
        }
        let costs = p
            .projects
            .lines
            .iter()
            .map(|l| l.row.tokens.estimated_cost_usd)
            .chain(
                p.projects
                    .lines
                    .iter()
                    .flat_map(|l| l.tools.iter().map(|t| t.2)),
            )
            .chain(p.totals.by_tool.iter().map(|t| t.tokens.estimated_cost_usd))
            .chain(
                p.sessions
                    .longest
                    .iter()
                    .chain(&p.sessions.costliest)
                    .map(|s| s.cost_usd),
            )
            .chain(
                p.spend
                    .periods
                    .iter()
                    .flat_map(|p| p.cells.iter().map(|c| c.0)),
            )
            .chain(
                p.spend_totals
                    .rows
                    .iter()
                    .chain(&p.spend_totals.columns)
                    .map(|c| c.0),
            );
        assert!(costs.flatten().all(|c| c.fract() == 0.0));
        let html = html::render(&p);
        for suffix in html.split('$').skip(1) {
            let amount: String = suffix
                .chars()
                .take_while(|c| c.is_ascii_digit() || *c == '.')
                .collect();
            assert!(!amount.contains('.'));
        }
        assert!(html.contains("Costs are estimates"));
        assert!(html.contains("estimated cost"));
    }

    #[test]
    fn dates_hours_export_note_and_safe_footer_stay() {
        let store = panel_fixture();
        let mut p = page(&store, Window::All);
        let (now, tz, first, facts, heatmap) = (
            p.now_label.clone(),
            p.tz_label.clone(),
            p.first_label.clone(),
            p.facts.clone(),
            p.heatmap.clone(),
        );
        Redaction::load(&store.conn, NOW)
            .unwrap()
            .apply(&mut p, &Window::ALL);
        assert_eq!(
            (
                p.now_label.clone(),
                p.tz_label.clone(),
                p.first_label.clone(),
                p.facts.clone()
            ),
            (now.clone(), tz, first, facts)
        );
        for (a, b) in p
            .heatmap
            .cells
            .iter()
            .flatten()
            .zip(heatmap.cells.iter().flatten())
        {
            assert_eq!((a.requests, a.tokens), (b.requests, b.tokens));
        }
        let html = html::render(&p);
        assert!(html.contains(&format!(
            "De-identified export. Generated {now}. Window: all time."
        )));
        assert!(html.contains("Data loaded in "));
        assert!(html.contains("Mon 00:00 to 01:00"));
    }
}
