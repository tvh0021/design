use chrono::Utc;
use rusqlite::params;
use usage_habit_report::{
    serve::{
        data::{MATRIX_ROWS, Window},
        html,
        routes::{self, Params},
    },
    store::Store,
};

#[test]
fn every_model_has_a_chart_with_filtered_counts_and_inherited_task_labels() {
    let store = Store::open_in_memory().unwrap();
    store.conn.execute_batch(
        "INSERT INTO sessions (tool, session_id, label) VALUES ('codex', 'root', 'other');
         INSERT INTO session_labels (tool, session_id, label, set_at) VALUES ('codex', 'root', 'review', 1);
         INSERT INTO sessions (tool, session_id, parent_session_id, is_subagent) VALUES ('codex', 'child', 'root', 1);
         INSERT INTO sessions (tool, session_id, label) VALUES ('antigravity', 'other-tool', 'coding');"
    ).unwrap();
    for i in 0..MATRIX_ROWS + 3 {
        store
            .conn
            .execute(
                "INSERT INTO requests (tool, request_key, session_id, ts_ms, canonical_model)
            VALUES ('codex', ?1, 'child', 1000, ?2)",
                params![format!("r{i}"), format!("gemini-test-{i}")],
            )
            .unwrap();
    }
    store
        .conn
        .execute_batch(
            "INSERT INTO requests (tool, request_key, session_id, ts_ms, canonical_model)
        VALUES ('antigravity', 'excluded', 'other-tool', 1000, 'claude-excluded');",
        )
        .unwrap();
    let page = routes::load_page(
        &store.conn,
        Params {
            window: Some(Window::All),
            tool: Some("codex"),
        },
        2000,
        &Utc,
        None,
    )
    .unwrap();
    let body = html::render(&page);
    assert_eq!(
        body.matches("<figure class=\"model-radar\"").count(),
        MATRIX_ROWS + 3
    );
    assert!(body.contains("gemini-test-0 task mix")); // the table folds this model
    assert!(body.contains("review: 1 requests, 100.0%"));
    assert!(body.contains("data-provider=\"google\"")); // model provider, not Codex host
    assert!(!body.contains("claude-excluded"));
    assert!(body.contains("Exact request counts by task kind"));
    assert_eq!(
        page.models.all_rows.iter().map(|r| r.total).sum::<i64>(),
        page.models.total
    );
}
