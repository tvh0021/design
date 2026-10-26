//! Canonical model names.
//!
//! Each harness spells the same model its own way: Claude Code logs
//! `claude-haiku-4-5-20251001`, Antigravity logs `claude-opus-5-5-high` or
//! enum-style `claude_v5_opus_thinking`, Codex logs `gpt-6-luna`. `canonical`
//! folds these onto one name so grouping and pricing agree, and
//! `litellm_keys` lists the LiteLLM price-table keys to try for a canonical
//! name, most specific first.

/// Raw names that the rules below cannot derive. Checked first.
/// Keys are lowercase. `gemini-pro-default` is Antigravity's label for the
/// default Gemini Pro of September 2026, which was Gemini 3.1 Pro.
const ALIASES: &[(&str, &str)] = &[("gemini-pro-default", "gemini-3.1-pro")];

/// Qualifiers that harnesses append to a model name to record the reasoning
/// effort or mode. They do not change the price. `max` is not stripped from
/// GPT names because `gpt-5.1-codex-max` is a distinct model.
const CLAUDE_QUALIFIERS: &[&str] = &["low", "medium", "high", "xhigh", "max", "thinking"];
/// `n`, `trial` and `c` appear on Antigravity's `gemini-3.8-flash-n`,
/// `gemini-4-argon-trial` and `gemini-4-argon-c`; each is the base model
/// under another label.
const GEMINI_QUALIFIERS: &[&str] = &[
    "low", "medium", "high", "minimal", "thinking", "tiered", "none", "n", "trial", "c",
];
const GPT_QUALIFIERS: &[&str] = &["low", "medium", "high", "xhigh", "minimal", "none"];

const CLAUDE_FAMILIES: &[&str] = &["opus", "sonnet", "haiku", "fable", "mythos"];

/// Prefixes some harnesses put in front of a model id.
const PREFIXES: &[&str] = &[
    "models/",
    "anthropic/",
    "openai/",
    "google/",
    "gemini/",
    "vertex_ai/",
    "model_",
    "model-",
];

/// Canonical name for a raw model string, or `None` when the name carries no
/// model identity (empty, or an opaque placeholder such as
/// `MODEL_PLACEHOLDER_M26`). Unknown but well-formed names come back
/// normalized; whether they have a price is decided by the rate table.
pub fn canonical(raw: &str) -> Option<String> {
    let mut s = raw.trim().to_ascii_lowercase();
    if s.is_empty() {
        return None;
    }
    if let Some((_, to)) = ALIASES.iter().find(|(from, _)| *from == s) {
        return Some((*to).to_string());
    }
    // Vertex AI version pins: `claude-opus-4-5@20251101`.
    if let Some(i) = s.find('@') {
        s.truncate(i);
    }
    while let Some(p) = PREFIXES.iter().find(|p| s.starts_with(**p)) {
        s = s[p.len()..].to_string();
    }
    if s.starts_with("placeholder") || s.is_empty() {
        return None;
    }
    if s.contains('_') && !s.contains('-') {
        s = from_enum(&s);
    }
    let mut parts: Vec<&str> = s.split('-').filter(|p| !p.is_empty()).collect();
    let qualifiers = match parts.first().copied() {
        Some("claude") => CLAUDE_QUALIFIERS,
        Some("gemini") => GEMINI_QUALIFIERS,
        Some(p) if p == "gpt" || is_o_series(p) => GPT_QUALIFIERS,
        _ => &[][..],
    };
    loop {
        let n = parts.len();
        // Claude snapshot date: `-20251001`.
        if n > 2 && is_digits(parts[n - 1], 8) {
            parts.pop();
            continue;
        }
        // OpenAI snapshot date: `-2025-08-07`.
        if n > 4
            && is_digits(parts[n - 3], 4)
            && is_digits(parts[n - 2], 2)
            && is_digits(parts[n - 1], 2)
        {
            parts.truncate(n - 3);
            continue;
        }
        if n > 2 && qualifiers.contains(&parts[n - 1]) {
            parts.pop();
            continue;
        }
        break;
    }
    Some(parts.join("-"))
}

/// LiteLLM keys to try for a canonical name, most specific first. The rate
/// table picks the first key it has.
pub fn litellm_keys(canonical: &str) -> Vec<String> {
    if canonical.starts_with("gemini-") {
        // Google AI Studio keys carry a `gemini/` prefix; Vertex AI keys do
        // not. Preview models are listed only under their `-preview` name.
        vec![
            format!("gemini/{canonical}"),
            canonical.to_string(),
            format!("gemini/{canonical}-preview"),
            format!("{canonical}-preview"),
        ]
    } else {
        vec![canonical.to_string()]
    }
}

/// `claude_v5_opus_thinking` -> `claude-opus-5-thinking`,
/// `claude_4_5_sonnet` -> `claude-sonnet-4-5`,
/// `gemini_3_8_flash_high` -> `gemini-3.8-flash-high`.
fn from_enum(s: &str) -> String {
    let toks: Vec<&str> = s.split('_').filter(|t| !t.is_empty()).collect();
    let version = |t: &str| -> Option<String> {
        let t = t.strip_prefix('v').unwrap_or(t);
        (!t.is_empty() && t.bytes().all(|b| b.is_ascii_digit())).then(|| t.to_string())
    };
    let nums: Vec<String> = toks.iter().filter_map(|t| version(t)).collect();
    let words: Vec<&str> = toks
        .iter()
        .copied()
        .filter(|t| version(t).is_none())
        .collect();
    match words.first().copied() {
        Some("claude") => {
            let family = words.iter().find(|w| CLAUDE_FAMILIES.contains(w));
            let rest = words.iter().skip(1).filter(|w| Some(*w) != family).copied();
            let mut out = vec!["claude".to_string()];
            out.extend(family.map(|f| f.to_string()));
            out.extend(nums);
            out.extend(rest.map(str::to_string));
            out.join("-")
        }
        Some(first) => {
            let mut out = vec![first.to_string()];
            if !nums.is_empty() {
                out.push(nums.join("."));
            }
            out.extend(words[1..].iter().map(|w| w.to_string()));
            out.join("-")
        }
        None => toks.join("-"),
    }
}

fn is_o_series(p: &str) -> bool {
    p.len() >= 2 && p.starts_with('o') && p[1..].bytes().all(|b| b.is_ascii_digit())
}

fn is_digits(s: &str, len: usize) -> bool {
    s.len() == len && s.bytes().all(|b| b.is_ascii_digit())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c(raw: &str) -> String {
        canonical(raw).unwrap_or_else(|| panic!("{raw} did not map"))
    }

    #[test]
    fn claude_code_names() {
        assert_eq!(c("claude-opus-5-5"), "claude-opus-5-5");
        assert_eq!(c("claude-fable-5-1"), "claude-fable-5-1");
        assert_eq!(c("claude-haiku-4-5-20251001"), "claude-haiku-4-5");
        assert_eq!(c("claude-sonnet-5-5"), "claude-sonnet-5-5");
        assert_eq!(c("claude-haiku-5-5"), "claude-haiku-5-5");
    }

    #[test]
    fn antigravity_names() {
        assert_eq!(c("claude-opus-5-5-high"), "claude-opus-5-5");
        assert_eq!(c("claude-opus-5-5-medium"), "claude-opus-5-5");
        assert_eq!(c("claude-opus-4-6-thinking"), "claude-opus-4-6");
        assert_eq!(c("gemini-3.8-flash-high"), "gemini-3.8-flash");
        assert_eq!(c("gemini-3.8-flash-tiered"), "gemini-3.8-flash");
        assert_eq!(c("gemini-3.8-flash-low"), "gemini-3.8-flash");
        assert_eq!(c("gemini-3.8-flash"), "gemini-3.8-flash");
        assert_eq!(c("gemini-4-argon-medium"), "gemini-4-argon");
        assert_eq!(c("gemini-3.8-flash-n"), "gemini-3.8-flash");
        assert_eq!(c("gemini-4-argon-trial"), "gemini-4-argon");
        assert_eq!(c("gemini-4-argon-c"), "gemini-4-argon");
        // No version, so no base model to fold into.
        assert_eq!(c("gemini-pro-default"), "gemini-3.1-pro");
        assert_eq!(c("codex-auto-review"), "codex-auto-review");
        assert_eq!(c("gemini-3.1-pro-low"), "gemini-3.1-pro");
        assert_eq!(c("gemini-3.5-flash-lite"), "gemini-3.5-flash-lite");
        assert_eq!(c("claude_v5_opus_thinking"), "claude-opus-5");
        assert_eq!(c("MODEL_CLAUDE_4_5_SONNET_THINKING"), "claude-sonnet-4-5");
        assert_eq!(c("gemini_3_8_flash_high"), "gemini-3.8-flash");
        assert_eq!(
            canonical("gemini-pro-default").as_deref(),
            Some("gemini-3.1-pro")
        );
        assert_eq!(canonical("MODEL_PLACEHOLDER_M26"), None);
        assert_eq!(canonical("  "), None);
    }

    #[test]
    fn codex_names() {
        assert_eq!(c("gpt-5.6-sol"), "gpt-5.6-sol");
        assert_eq!(c("gpt-6-luna"), "gpt-6-luna");
        assert_eq!(c("gpt-6.1-sol"), "gpt-6.1-sol");
        assert_eq!(c("gpt-5.1-codex-max"), "gpt-5.1-codex-max");
        assert_eq!(c("gpt-5-2025-08-07"), "gpt-5");
        assert_eq!(c("o4-mini-2025-04-16"), "o4-mini");
        assert_eq!(c("openai/gpt-6-sol"), "gpt-6-sol");
    }

    #[test]
    fn provider_prefixes_and_pins() {
        assert_eq!(c("vertex_ai/claude-opus-4-5@20251101"), "claude-opus-4-5");
        assert_eq!(c("models/gemini-3.8-flash"), "gemini-3.8-flash");
        assert_eq!(c("Claude-Opus-5-5"), "claude-opus-5-5");
    }

    #[test]
    fn keys_for_canonical() {
        assert_eq!(litellm_keys("claude-opus-5-5"), vec!["claude-opus-5-5"]);
        assert_eq!(litellm_keys("gpt-6-luna"), vec!["gpt-6-luna"]);
        assert_eq!(
            litellm_keys("gemini-3.1-pro"),
            vec![
                "gemini/gemini-3.1-pro",
                "gemini-3.1-pro",
                "gemini/gemini-3.1-pro-preview",
                "gemini-3.1-pro-preview",
            ]
        );
    }
}
