//! Model prices and the price step.
//!
//! Prices come from LiteLLM's public table, cut down to the providers whose
//! models the harnesses use (see `PROVIDERS`) and to the per-token fields
//! this tool reads. The cut is stored as `~/.config/usage-habit-report/rates.json`
//! and refreshed when older than `MAX_AGE_MS`. A copy taken when the binary
//! was built (`rates_snapshot.json`) covers first run and offline use.

use crate::models;
use anyhow::{Context, Result, bail};
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::time::Duration;

pub const SOURCE_URL: &str =
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json";

/// LiteLLM providers kept: Anthropic (Claude), OpenAI (GPT, o-series),
/// Google AI Studio (`gemini/…` keys) and Vertex AI language models (bare
/// `gemini-…` keys, which hold some Gemini models AI Studio lacks).
/// Re-hosted copies of the same models (Bedrock, Azure, Vertex AI Anthropic,
/// OpenRouter, …) are left out: they repeat first-party prices or add markups.
pub const PROVIDERS: &[&str] = &["anthropic", "openai", "gemini", "vertex_ai-language-models"];

/// LiteLLM `mode` values kept. Image, audio, embedding and realtime models
/// are priced per image/second/character and never appear in harness logs.
const MODES: &[&str] = &["chat", "responses", "completion"];

/// Per-token fields kept from each entry.
const BASE_FIELDS: &[&str] = &[
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "cache_creation_input_token_cost_above_1hr",
];

/// Refresh `rates.json` when it is older than this.
pub const MAX_AGE_MS: i64 = 7 * 24 * 3_600_000;

const FETCH_TIMEOUT: Duration = Duration::from_secs(10);

/// Filtered LiteLLM table as written by `rates update` and compiled in.
pub const SNAPSHOT_JSON: &str = include_str!("rates_snapshot.json");

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RatesFile {
    /// RFC 3339 UTC time of the fetch.
    pub fetched_at: String,
    pub source: String,
    /// LiteLLM key -> kept fields (`litellm_provider`, `mode`, cost fields).
    pub models: BTreeMap<String, Map<String, Value>>,
}

impl RatesFile {
    pub fn fetched_at_ms(&self) -> Option<i64> {
        chrono::DateTime::parse_from_rfc3339(&self.fetched_at)
            .ok()
            .map(|d| d.timestamp_millis())
    }

    pub fn snapshot() -> Result<Self> {
        serde_json::from_str(SNAPSHOT_JSON).context("parsing compiled-in rates snapshot")
    }

    /// Cut LiteLLM's full table down to `PROVIDERS`, `MODES`, and the cost
    /// fields this tool reads. Entries without both an input and an output
    /// per-token price are dropped.
    pub fn from_litellm(raw: &str, fetched_at_ms: i64) -> Result<Self> {
        let all: Map<String, Value> =
            serde_json::from_str(raw).context("LiteLLM price table is not a JSON object")?;
        let mut models = BTreeMap::new();
        for (key, entry) in all {
            let Some(entry) = entry.as_object() else {
                continue;
            };
            let provider = entry.get("litellm_provider").and_then(Value::as_str);
            if !provider.is_some_and(|p| PROVIDERS.contains(&p)) {
                continue;
            }
            let mode = entry.get("mode").and_then(Value::as_str).unwrap_or("chat");
            if !MODES.contains(&mode) {
                continue;
            }
            let kept: Map<String, Value> = entry
                .iter()
                .filter(|(f, v)| {
                    (f.as_str() == "litellm_provider" || f.as_str() == "mode")
                        || (v.is_number()
                            && (BASE_FIELDS.contains(&f.as_str()) || tier_of(f).is_some()))
                })
                .map(|(f, v)| (f.clone(), v.clone()))
                .collect();
            if Price::from_entry(&kept).is_some() {
                models.insert(key, kept);
            }
        }
        if models.is_empty() {
            bail!("LiteLLM price table had no {PROVIDERS:?} chat models");
        }
        Ok(RatesFile {
            fetched_at: crate::query::rfc3339(fetched_at_ms),
            source: SOURCE_URL.to_string(),
            models,
        })
    }

    /// Price for a raw model name, with the canonical name and the LiteLLM
    /// key it resolved to.
    pub fn lookup(&self, raw: &str) -> Lookup {
        let Some(canonical) = models::canonical(raw) else {
            return Lookup::default();
        };
        for key in models::litellm_keys(&canonical) {
            if let Some(price) = self.models.get(&key).and_then(Price::from_entry) {
                return Lookup {
                    canonical: Some(canonical),
                    key: Some(key),
                    price: Some(price),
                };
            }
        }
        Lookup {
            canonical: Some(canonical),
            ..Lookup::default()
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Lookup {
    pub canonical: Option<String>,
    pub key: Option<String>,
    pub price: Option<Price>,
}

/// Where the rates in use came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    /// Fetched during this command.
    Fetched,
    /// `rates.json` on disk.
    File,
    /// Compiled into the binary.
    Snapshot,
}

#[derive(Debug, Clone)]
pub struct Rates {
    pub file: RatesFile,
    pub origin: Origin,
}

/// Fetch LiteLLM's table over HTTPS (blocking, 10 s timeout).
pub fn fetch_litellm() -> Result<String> {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(FETCH_TIMEOUT))
        .build()
        .into();
    let mut resp = agent
        .get(SOURCE_URL)
        .call()
        .context("fetching LiteLLM prices")?;
    resp.body_mut()
        .with_config()
        .limit(64 * 1024 * 1024)
        .read_to_string()
        .context("reading LiteLLM prices")
}

/// `rates update`: fetch, filter, write `path`. Errors when the fetch fails.
pub fn update(
    path: &Path,
    now_ms: i64,
    fetch: impl FnOnce() -> Result<String>,
) -> Result<RatesFile> {
    let file = RatesFile::from_litellm(&fetch()?, now_ms)?;
    write(path, &file)?;
    Ok(file)
}

fn write(path: &Path, file: &RatesFile) -> Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(file)? + "\n")
        .with_context(|| format!("writing {}", tmp.display()))?;
    std::fs::rename(&tmp, path).with_context(|| format!("writing {}", path.display()))?;
    Ok(())
}

fn read(path: &Path) -> Option<RatesFile> {
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

/// Rates for a command that needs prices. When `path` is missing, unreadable,
/// or older than `MAX_AGE_MS`, and `offline` is false, announce the refresh on
/// stderr and fetch. A failed fetch falls back to the file, then to the
/// compiled-in snapshot, with a note on stderr. Never fails.
pub fn load(
    path: &Path,
    offline: bool,
    now_ms: i64,
    fetch: impl FnOnce() -> Result<String>,
) -> Rates {
    let on_disk = read(path);
    let fresh = on_disk
        .as_ref()
        .and_then(RatesFile::fetched_at_ms)
        .is_some_and(|t| now_ms - t < MAX_AGE_MS);
    if !fresh && !offline {
        eprintln!("refreshing model rates from LiteLLM…");
        match update(path, now_ms, fetch) {
            Ok(file) => {
                return Rates {
                    file,
                    origin: Origin::Fetched,
                };
            }
            Err(e) => {
                let using = match &on_disk {
                    Some(f) => format!("{} from {}", path.display(), f.fetched_at),
                    None => "built-in snapshot".to_string(),
                };
                eprintln!("rate refresh failed ({e:#}); using {using}");
            }
        }
    }
    match on_disk {
        Some(file) => Rates {
            file,
            origin: Origin::File,
        },
        None => Rates {
            file: RatesFile::snapshot().expect("compiled-in snapshot parses (tested)"),
            origin: Origin::Snapshot,
        },
    }
}

/// One tier of a tiered price: rates that replace the base rates when the
/// prompt is larger than `above` tokens.
#[derive(Debug, Clone, PartialEq)]
pub struct Tier {
    pub above: i64,
    pub input: Option<f64>,
    pub output: Option<f64>,
    pub cache_read: Option<f64>,
    pub cache_creation: Option<f64>,
}

/// USD per token for one model.
#[derive(Debug, Clone, PartialEq)]
pub struct Price {
    pub input: f64,
    pub output: f64,
    pub cache_read: Option<f64>,
    /// 5-minute cache write. The 1-hour rate is not used: no harness records
    /// the cache TTL of a write.
    pub cache_creation: Option<f64>,
    /// Ascending by `above`.
    pub tiers: Vec<Tier>,
}

/// Token counts of one request. `input` is uncached input; output already
/// includes reasoning tokens.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Usage {
    pub input: i64,
    pub cache_read: i64,
    pub cache_creation: i64,
    pub output: i64,
}

impl Price {
    pub fn from_entry(e: &Map<String, Value>) -> Option<Price> {
        let f = |k: &str| e.get(k).and_then(Value::as_f64);
        let mut tiers: BTreeMap<i64, Tier> = BTreeMap::new();
        for (field, v) in e {
            let (Some((base, above)), Some(v)) = (tier_of(field), v.as_f64()) else {
                continue;
            };
            let t = tiers.entry(above).or_insert(Tier {
                above,
                input: None,
                output: None,
                cache_read: None,
                cache_creation: None,
            });
            match base {
                "input_cost_per_token" => t.input = Some(v),
                "output_cost_per_token" => t.output = Some(v),
                "cache_read_input_token_cost" => t.cache_read = Some(v),
                "cache_creation_input_token_cost" => t.cache_creation = Some(v),
                _ => {}
            }
        }
        Some(Price {
            input: f("input_cost_per_token")?,
            output: f("output_cost_per_token")?,
            cache_read: f("cache_read_input_token_cost"),
            cache_creation: f("cache_creation_input_token_cost"),
            tiers: tiers.into_values().collect(),
        })
    }

    /// Estimated USD for one request. The tier is chosen by prompt size
    /// (uncached + cache-read + cache-write input), as the providers bill it.
    /// A missing cache rate falls back to the input rate of the same tier.
    pub fn cost(&self, u: Usage) -> f64 {
        let prompt = u.input + u.cache_read + u.cache_creation;
        let tier = self.tiers.iter().rev().find(|t| prompt > t.above);
        let input = tier.and_then(|t| t.input).unwrap_or(self.input);
        let output = tier.and_then(|t| t.output).unwrap_or(self.output);
        let cache_read = tier
            .and_then(|t| t.cache_read)
            .or(self.cache_read)
            .unwrap_or(input);
        let cache_creation = tier
            .and_then(|t| t.cache_creation)
            .or(self.cache_creation)
            .unwrap_or(input);
        u.input as f64 * input
            + u.cache_read as f64 * cache_read
            + u.cache_creation as f64 * cache_creation
            + u.output as f64 * output
    }
}

/// `input_cost_per_token_above_200k_tokens` -> (`input_cost_per_token`, 200000).
/// Variants with a further suffix (`_priority`, `_batches`, `_flex`, …) and
/// the 1-hour cache tier are not tiers.
fn tier_of(field: &str) -> Option<(&str, i64)> {
    let (base, rest) = field.split_once("_above_")?;
    if !matches!(
        base,
        "input_cost_per_token"
            | "output_cost_per_token"
            | "cache_read_input_token_cost"
            | "cache_creation_input_token_cost"
    ) {
        return None;
    }
    let n = rest.strip_suffix("k_tokens")?;
    let k: i64 = n.parse().ok()?;
    Some((base, k * 1000))
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct PriceStats {
    /// Rows the step looked at.
    pub rows: u64,
    /// Rows that now have a cost.
    pub priced: u64,
    /// Rows left with a null cost (no model, or no price for it).
    pub unpriced: u64,
}

/// Fill `canonical_model` and `cost_usd`. With `all = false` only rows where
/// either is null are touched (new rows, rows changed since they were priced,
/// and rows whose model had no price); with `all = true` every row is
/// re-priced (after the rates changed). A row whose model has no price keeps
/// a null cost.
pub fn price_requests(conn: &mut Connection, rates: &RatesFile, all: bool) -> Result<PriceStats> {
    let tx = conn.transaction()?;
    let mut stats = PriceStats::default();
    {
        let mut select = tx.prepare(&format!(
            "SELECT id, model, input_tokens, cache_read_input_tokens,
                    cache_creation_input_tokens, output_tokens
             FROM requests {}",
            if all {
                ""
            } else {
                "WHERE canonical_model IS NULL OR cost_usd IS NULL"
            }
        ))?;
        let mut update =
            tx.prepare("UPDATE requests SET canonical_model = ?2, cost_usd = ?3 WHERE id = ?1")?;
        let mut cache: HashMap<String, Lookup> = HashMap::new();
        let mut rows = select.query([])?;
        while let Some(r) = rows.next()? {
            let id: i64 = r.get(0)?;
            let model: Option<String> = r.get(1)?;
            let usage = Usage {
                input: r.get(2)?,
                cache_read: r.get(3)?,
                cache_creation: r.get(4)?,
                output: r.get(5)?,
            };
            let lookup = match &model {
                Some(m) => cache
                    .entry(m.clone())
                    .or_insert_with(|| rates.lookup(m))
                    .clone(),
                None => Lookup::default(),
            };
            let cost = lookup.price.as_ref().map(|p| p.cost(usage));
            update.execute(params![id, lookup.canonical, cost])?;
            stats.rows += 1;
            if cost.is_some() {
                stats.priced += 1;
            } else {
                stats.unpriced += 1;
            }
        }
    }
    tx.commit()?;
    Ok(stats)
}

/// One line of `query models`.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ModelRow {
    pub tool: String,
    pub model: Option<String>,
    pub canonical_model: Option<String>,
    pub litellm_key: Option<String>,
    pub priced: bool,
    pub requests: i64,
}

/// Every (tool, raw model) in the store with its canonical name and whether
/// it has a price, unpriced first.
pub fn model_rows(conn: &Connection, rates: &RatesFile) -> Result<Vec<ModelRow>> {
    let mut stmt = conn.prepare(
        "SELECT tool, model, COUNT(*) FROM requests GROUP BY tool, model ORDER BY tool, COUNT(*) DESC",
    )?;
    let mut rows = stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, Option<String>>(1)?,
                r.get::<_, i64>(2)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?
        .into_iter()
        .map(|(tool, model, requests)| {
            let l = model
                .as_deref()
                .map(|m| rates.lookup(m))
                .unwrap_or_default();
            ModelRow {
                tool,
                model,
                canonical_model: l.canonical,
                litellm_key: l.key,
                priced: l.price.is_some(),
                requests,
            }
        })
        .collect::<Vec<_>>();
    rows.sort_by_key(|r| r.priced);
    Ok(rows)
}

pub fn render_models_table(rows: &[ModelRow]) -> String {
    let mut table: Vec<[String; 6]> = vec![[
        "tool".into(),
        "model".into(),
        "canonical".into(),
        "litellm_key".into(),
        "priced".into(),
        "requests".into(),
    ]];
    let dash = |s: &Option<String>| s.clone().unwrap_or_else(|| "-".into());
    for r in rows {
        table.push([
            r.tool.clone(),
            r.model.clone().unwrap_or_else(|| "(none)".into()),
            dash(&r.canonical_model),
            dash(&r.litellm_key),
            if r.priced { "yes" } else { "no" }.into(),
            r.requests.to_string(),
        ]);
    }
    let widths: Vec<usize> = (0..6)
        .map(|i| {
            table
                .iter()
                .map(|r| r[i].chars().count())
                .max()
                .unwrap_or(0)
        })
        .collect();
    let mut out = String::new();
    for r in table {
        for (i, cell) in r.iter().enumerate() {
            if i == 5 {
                out.push_str(&format!("{:>w$}", cell, w = widths[i]));
            } else {
                out.push_str(&format!("{:<w$}  ", cell, w = widths[i]));
            }
        }
        out.push('\n');
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    const M: f64 = 1e-6;

    fn price(json: Value) -> Price {
        Price::from_entry(json.as_object().unwrap()).unwrap()
    }

    fn opus() -> Price {
        price(serde_json::json!({
            "input_cost_per_token": 4.0 * M,
            "output_cost_per_token": 20.0 * M,
            "cache_read_input_token_cost": 0.2 * M,
            "cache_creation_input_token_cost": 5.0 * M,
            "cache_creation_input_token_cost_above_1hr": 8.0 * M,
        }))
    }

    fn close(a: f64, b: f64) {
        assert!((a - b).abs() < 1e-12, "{a} != {b}");
    }

    #[test]
    fn cost_per_token_kind() {
        let p = opus();
        let u = |input, cache_read, cache_creation, output| Usage {
            input,
            cache_read,
            cache_creation,
            output,
        };
        close(p.cost(u(1_000_000, 0, 0, 0)), 4.0);
        close(p.cost(u(0, 1_000_000, 0, 0)), 0.2);
        // 5-minute write rate, not the 1-hour one.
        close(p.cost(u(0, 0, 1_000_000, 0)), 5.0);
        close(p.cost(u(0, 0, 0, 1_000_000)), 20.0);
        close(
            p.cost(u(10, 100, 1000, 50)),
            10.0 * 4e-6 + 100.0 * 2e-7 + 1000.0 * 5e-6 + 50.0 * 2e-5,
        );
        close(p.cost(Usage::default()), 0.0);
    }

    #[test]
    fn tiered_cost_applies_above_threshold() {
        let p = price(serde_json::json!({
            "input_cost_per_token": 2.0 * M,
            "output_cost_per_token": 12.0 * M,
            "cache_read_input_token_cost": 0.2 * M,
            "input_cost_per_token_above_200k_tokens": 4.0 * M,
            "output_cost_per_token_above_200k_tokens": 18.0 * M,
            "cache_read_input_token_cost_above_200k_tokens": 0.4 * M,
            "input_cost_per_token_above_200k_tokens_priority": 99.0 * M,
        }));
        assert_eq!(p.tiers.len(), 1);
        assert_eq!(p.tiers[0].above, 200_000);
        // Exactly at the threshold: base rates.
        let at = Usage {
            input: 100_000,
            cache_read: 100_000,
            cache_creation: 0,
            output: 1_000,
        };
        close(
            p.cost(at),
            100_000.0 * 2e-6 + 100_000.0 * 2e-7 + 1_000.0 * 12e-6,
        );
        // One token over, counting cached input: every token at the tier rate.
        let over = Usage {
            input: 100_001,
            ..at
        };
        close(
            p.cost(over),
            100_001.0 * 4e-6 + 100_000.0 * 4e-7 + 1_000.0 * 18e-6,
        );
    }

    #[test]
    fn missing_cache_rates_fall_back_to_input() {
        let p = price(serde_json::json!({
            "input_cost_per_token": 1.0 * M,
            "output_cost_per_token": 2.0 * M,
        }));
        let u = Usage {
            input: 1,
            cache_read: 2,
            cache_creation: 3,
            output: 4,
        };
        close(p.cost(u), 6.0 * M + 8.0 * M);
    }

    #[test]
    fn tier_field_names() {
        assert_eq!(
            tier_of("input_cost_per_token_above_272k_tokens"),
            Some(("input_cost_per_token", 272_000))
        );
        assert_eq!(tier_of("input_cost_per_token_above_272k_tokens_flex"), None);
        assert_eq!(tier_of("cache_creation_input_token_cost_above_1hr"), None);
        assert_eq!(
            tier_of("input_cost_per_audio_token_above_128k_tokens"),
            None
        );
    }

    fn litellm_sample() -> String {
        serde_json::json!({
            "sample_spec": {"litellm_provider": "one of …", "input_cost_per_token": 0.0},
            "claude-opus-5-5": {
                "litellm_provider": "anthropic", "mode": "chat",
                "input_cost_per_token": 4.0 * M, "output_cost_per_token": 20.0 * M,
                "cache_read_input_token_cost": 0.2 * M,
                "cache_creation_input_token_cost": 5.0 * M,
                "cache_creation_input_token_cost_above_1hr": 8.0 * M,
                "input_cost_per_token_batches": 2.0 * M,
                "supports_vision": true
            },
            "gemini/gemini-3.8-flash": {
                "litellm_provider": "gemini", "mode": "chat",
                "input_cost_per_token": 0.75 * M, "output_cost_per_token": 3.75 * M
            },
            "gemini/gemini-3.1-pro-preview": {
                "litellm_provider": "gemini", "mode": "chat",
                "input_cost_per_token": 2.0 * M, "output_cost_per_token": 12.0 * M
            },
            "bedrock/claude-opus-5-5": {
                "litellm_provider": "bedrock", "mode": "chat",
                "input_cost_per_token": 9.0 * M, "output_cost_per_token": 9.0 * M
            },
            "gpt-image-9": {
                "litellm_provider": "openai", "mode": "image_generation",
                "input_cost_per_token": 1.0 * M, "output_cost_per_token": 1.0 * M
            }
        })
        .to_string()
    }

    #[test]
    fn filter_keeps_providers_modes_and_cost_fields() {
        let f = RatesFile::from_litellm(&litellm_sample(), 0).unwrap();
        assert_eq!(
            f.models.keys().collect::<Vec<_>>(),
            vec![
                "claude-opus-5-5",
                "gemini/gemini-3.1-pro-preview",
                "gemini/gemini-3.8-flash"
            ]
        );
        let opus = &f.models["claude-opus-5-5"];
        assert!(opus.contains_key("cache_creation_input_token_cost_above_1hr"));
        assert!(!opus.contains_key("input_cost_per_token_batches"));
        assert!(!opus.contains_key("supports_vision"));
        assert_eq!(f.fetched_at, "1970-01-01T00:00:00Z");
    }

    #[test]
    fn lookup_maps_raw_names_to_keys() {
        let f = RatesFile::from_litellm(&litellm_sample(), 0).unwrap();
        let l = f.lookup("claude-opus-5-5-high");
        assert_eq!(l.canonical.as_deref(), Some("claude-opus-5-5"));
        assert_eq!(l.key.as_deref(), Some("claude-opus-5-5"));
        assert!(l.price.is_some());
        let l = f.lookup("gemini-3.8-flash-medium");
        assert_eq!(l.key.as_deref(), Some("gemini/gemini-3.8-flash"));
        let l = f.lookup("gemini-3.1-pro-low");
        assert_eq!(l.key.as_deref(), Some("gemini/gemini-3.1-pro-preview"));
        let l = f.lookup("codex-auto-review");
        assert_eq!(l.canonical.as_deref(), Some("codex-auto-review"));
        assert_eq!((l.key, l.price), (None, None));
        assert_eq!(f.lookup("MODEL_PLACEHOLDER_M26"), Lookup::default());
    }

    fn insert(conn: &Connection, key: &str, model: Option<&str>, tokens: [i64; 4]) {
        conn.execute(
            "INSERT INTO requests (tool, request_key, session_id, ts_ms, model, input_tokens,
                 cache_read_input_tokens, cache_creation_input_tokens, output_tokens)
             VALUES ('t', ?1, 's', 0, ?2, ?3, ?4, ?5, ?6)",
            params![key, model, tokens[0], tokens[1], tokens[2], tokens[3]],
        )
        .unwrap();
    }

    fn cost_of(conn: &Connection, key: &str) -> (Option<String>, Option<f64>) {
        conn.query_row(
            "SELECT canonical_model, cost_usd FROM requests WHERE request_key = ?1",
            [key],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap()
    }

    #[test]
    fn unpriced_model_gets_null_cost() {
        let mut store = Store::open_in_memory().unwrap();
        let f = RatesFile::from_litellm(&litellm_sample(), 0).unwrap();
        insert(
            &store.conn,
            "a",
            Some("claude-opus-5-5"),
            [1_000_000, 0, 0, 0],
        );
        insert(
            &store.conn,
            "b",
            Some("codex-auto-review"),
            [1_000_000, 0, 0, 0],
        );
        insert(&store.conn, "c", None, [1_000_000, 0, 0, 0]);
        let s = price_requests(&mut store.conn, &f, false).unwrap();
        assert_eq!((s.rows, s.priced, s.unpriced), (3, 1, 2));
        let (canon, cost) = cost_of(&store.conn, "a");
        assert_eq!(canon.as_deref(), Some("claude-opus-5-5"));
        close(cost.unwrap(), 4.0);
        assert_eq!(
            cost_of(&store.conn, "b"),
            (Some("codex-auto-review".into()), None)
        );
        assert_eq!(cost_of(&store.conn, "c"), (None, None));

        // Only rows still missing a cost are revisited.
        let s = price_requests(&mut store.conn, &f, false).unwrap();
        assert_eq!((s.rows, s.priced), (2, 0));
        let s = price_requests(&mut store.conn, &f, true).unwrap();
        assert_eq!(s.rows, 3);
    }

    #[test]
    fn changed_tokens_clear_the_cost() {
        let mut store = Store::open_in_memory().unwrap();
        let f = RatesFile::from_litellm(&litellm_sample(), 0).unwrap();
        insert(&store.conn, "a", Some("claude-opus-5-5"), [0, 0, 0, 1_000]);
        price_requests(&mut store.conn, &f, false).unwrap();
        // A no-op merge keeps the cost.
        store
            .conn
            .execute(
                "UPDATE requests SET output_tokens = MAX(output_tokens, 10)",
                [],
            )
            .unwrap();
        assert!(cost_of(&store.conn, "a").1.is_some());
        // A streaming merge that grows the output clears it for re-pricing.
        store
            .conn
            .execute(
                "UPDATE requests SET output_tokens = MAX(output_tokens, 1000000)",
                [],
            )
            .unwrap();
        assert_eq!(cost_of(&store.conn, "a"), (None, None));
        price_requests(&mut store.conn, &f, false).unwrap();
        close(cost_of(&store.conn, "a").1.unwrap(), 20.0);
    }

    #[test]
    fn snapshot_parses_and_covers_each_family() {
        let f = RatesFile::snapshot().unwrap();
        assert!(f.fetched_at_ms().is_some());
        // Every name seen in the three harnesses' logs that has a public price.
        for raw in [
            // Claude Code
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
            "claude-fable-5-1",
            "claude-haiku-4-5-20251001",
            // Codex
            "gpt-6.1-sol",
            "gpt-6-luna",
            "gpt-6-sol",
            "gpt-6-astra",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "gpt-5.3-codex",
            // Antigravity
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-tiered",
            "gemini-4-argon-medium",
            "gemini-3.1-pro-low",
            "claude-opus-5-5-high",
            "claude-opus-4-6-thinking",
            "claude_v5_opus_thinking",
        ] {
            assert!(
                f.lookup(raw).price.is_some(),
                "{raw} not priced by snapshot"
            );
        }
    }

    #[test]
    fn load_falls_back_when_fetch_fails() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("rates.json");
        let fail = || -> Result<String> { bail!("offline") };
        let never = || -> Result<String> { panic!("must not fetch") };

        // No file, fetch fails: compiled-in snapshot.
        let r = load(&path, false, 0, fail);
        assert_eq!(r.origin, Origin::Snapshot);
        assert!(!r.file.models.is_empty());
        // No file, offline: snapshot, no fetch attempted.
        assert_eq!(load(&path, true, 0, never).origin, Origin::Snapshot);

        // Fetch succeeds: file written with its timestamp.
        let day = 86_400_000;
        let r = load(&path, false, 10 * day, || Ok(litellm_sample()));
        assert_eq!(r.origin, Origin::Fetched);
        assert!(path.is_file());
        // Fresh file: used as is.
        assert_eq!(load(&path, false, 16 * day, never).origin, Origin::File);
        // Stale file, fetch fails: the stale file, not the snapshot.
        let r = load(&path, false, 17 * day, fail);
        assert_eq!(r.origin, Origin::File);
        assert_eq!(r.file.models.len(), 3);
        // Stale file, offline: no fetch.
        assert_eq!(load(&path, true, 30 * day, never).origin, Origin::File);

        // Corrupt file, fetch fails: snapshot.
        std::fs::write(&path, "{not json").unwrap();
        assert_eq!(load(&path, false, 0, fail).origin, Origin::Snapshot);
    }
}
