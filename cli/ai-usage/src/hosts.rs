//! Readings from other machines: the hosts config, fetching each host's
//! `ai-usage --local --json` over ssh, and merging every host's reports so
//! one account signed in on several machines shows once.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::report::{HOST_PROVIDER, HostRef, Report};

/// How long one host may take, ssh connection included, before it is killed.
const HOST_TIMEOUT: Duration = Duration::from_secs(25);

/// A machine to ask over ssh.
#[derive(Debug, PartialEq)]
pub struct Host {
    pub name: String,
    pub ssh: String,
    pub command: String,
    pub t3_environment: Option<String>,
}

impl Host {
    pub fn host_ref(&self) -> HostRef {
        HostRef { name: self.name.clone(), t3_environment: self.t3_environment.clone() }
    }
}

#[derive(Debug, PartialEq)]
pub struct Config {
    /// This machine.
    pub me: HostRef,
    pub hosts: Vec<Host>,
}

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct RawSelf {
    name: Option<String>,
    t3_environment: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawHost {
    name: String,
    ssh: Option<String>,
    command: Option<String>,
    t3_environment: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawConfig {
    #[serde(rename = "self", default)]
    me: RawSelf,
    #[serde(default)]
    hosts: Vec<RawHost>,
}

fn trimmed(s: Option<String>) -> Option<String> {
    s.map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

/// `$XDG_CONFIG_HOME/ai-usage/hosts.json`, else `~/.config/ai-usage/hosts.json`, on macOS too.
pub fn config_path_from(xdg_config_home: Option<&str>, home: &Path) -> PathBuf {
    xdg_config_home
        .map(str::trim)
        .filter(|d| !d.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"))
        .join("ai-usage/hosts.json")
}

/// The config in `text`, or this machine alone when there is no file.
/// `self.name` defaults to `hostname`, a host's `ssh` to its name and
/// `command` to `ai-usage`.
pub fn parse_config(text: Option<&str>, hostname: &str) -> Result<Config, String> {
    let raw = match text {
        Some(t) => serde_json::from_str::<RawConfig>(t).map_err(|e| e.to_string())?,
        None => RawConfig { me: RawSelf::default(), hosts: Vec::new() },
    };
    let me = HostRef {
        name: trimmed(raw.me.name).unwrap_or_else(|| hostname.to_string()),
        t3_environment: trimmed(raw.me.t3_environment),
    };
    let mut hosts = Vec::new();
    for h in raw.hosts {
        let name = trimmed(Some(h.name)).ok_or("a host has an empty name")?;
        if name == me.name || hosts.iter().any(|o: &Host| o.name == name) {
            return Err(format!("host name {name} is used twice"));
        }
        let ssh = trimmed(h.ssh).unwrap_or_else(|| name.clone());
        if ssh.starts_with('-') {
            return Err(format!("host {name}: ssh destination {ssh} starts with '-'"));
        }
        let command = trimmed(h.command).unwrap_or_else(|| "ai-usage".into());
        if command.contains(['\n', '\r']) {
            return Err(format!("host {name}: command contains a line break"));
        }
        hosts.push(Host { ssh, command, t3_environment: trimmed(h.t3_environment), name });
    }
    Ok(Config { me, hosts })
}

/// This machine's short host name, as `hostname -s` would print it.
fn short_hostname() -> String {
    Command::new("hostname")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .and_then(|h| h.split('.').next().map(str::to_string))
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| "localhost".into())
}

/// This machine alone, named by its host name, without reading any config.
pub fn local_config() -> Config {
    Config { me: HostRef { name: short_hostname(), t3_environment: None }, hosts: Vec::new() }
}

pub fn load_config() -> Result<Config, String> {
    let home = PathBuf::from(std::env::var("HOME").unwrap_or_default());
    let path = config_path_from(std::env::var("XDG_CONFIG_HOME").ok().as_deref(), &home);
    let text = match std::fs::read_to_string(&path) {
        Ok(t) => Some(t),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => return Err(format!("{}: {e}", path.display())),
    };
    parse_config(text.as_deref(), &short_hostname()).map_err(|e| format!("{}: {e}", path.display()))
}

/// Runs `ai-usage --local --json` on `host` over ssh, killing it after `HOST_TIMEOUT`.
pub fn fetch(host: &Host, providers: &[String]) -> Result<Vec<Report>, String> {
    let mut child = Command::new("ssh")
        .args(["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", &host.ssh, &host.command, "--local", "--json"])
        .args(providers)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("unreachable (could not run ssh: {e})"))?;
    let drain = |pipe: Option<Box<dyn Read + Send>>| {
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            if let Some(mut p) = pipe {
                let _ = p.read_to_end(&mut buf);
            }
            buf
        })
    };
    let out = drain(child.stdout.take().map(|p| Box::new(p) as Box<dyn Read + Send>));
    let err = drain(child.stderr.take().map(|p| Box::new(p) as Box<dyn Read + Send>));

    let deadline = Instant::now() + HOST_TIMEOUT;
    let code = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status.code()),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                break None;
            }
        }
    };
    let (out, err) = (out.join().unwrap_or_default(), err.join().unwrap_or_default());
    match code {
        None => Err(format!("timed out after {}s", HOST_TIMEOUT.as_secs())),
        Some(code) => reports_from(code, &String::from_utf8_lossy(&out), &String::from_utf8_lossy(&err)),
    }
}

/// A host's reports from its ssh exit code (`None`: killed by a signal) and
/// output, or why there are none. Any non-zero exit is a failure, even with
/// output: the remote never runs with `--fail-above`.
pub fn reports_from(code: Option<i32>, stdout: &str, stderr: &str) -> Result<Vec<Report>, String> {
    if code == Some(0) {
        return serde_json::from_str(stdout).map_err(|e| format!("unreadable ai-usage output there ({e})"));
    }
    let last = stderr.lines().map(str::trim).filter(|l| !l.is_empty()).last().unwrap_or("no output");
    Err(if code == Some(255) {
        format!("unreachable (ssh: {})", last.strip_prefix("ssh: ").unwrap_or(last))
    } else if stderr.contains("unknown option --local") {
        "ai-usage too old, update it there".into()
    } else if code == Some(127) || stderr.contains("command not found") {
        "ai-usage not found there".into()
    } else if let Some(code) = code {
        let first = stderr.lines().map(str::trim).find(|l| !l.is_empty());
        format!("ai-usage failed there: {}", first.map_or(format!("exit {code}"), str::to_string))
    } else {
        "ssh was killed by a signal".into()
    })
}

/// Where a provider sits in the usual order; unknown ones go last.
fn provider_rank(name: &str) -> usize {
    crate::providers::PROVIDERS.iter().position(|(p, _)| *p == name).unwrap_or(usize::MAX)
}

/// Every host's reports as one list. `local` are this machine's, already
/// tagged with it; each remote host's reports are tagged with that host.
///
/// Within each provider (in the usual order, this machine first): a host
/// that merely has no sign-in is left out when any host has one; reports of
/// the same account collapse into one listing every host, keeping the first
/// reading unless it is stale and a later one is not; reports
/// with no known account never merge. A host that could not be read becomes
/// one `Host` element at the end.
pub fn merge(local: Vec<Report>, remotes: Vec<(HostRef, Result<Vec<Report>, String>)>) -> Vec<Report> {
    let mut all = local;
    let mut failed = Vec::new();
    for (host, result) in remotes {
        match result {
            Ok(reports) => all.extend(reports.into_iter().filter(|r| r.provider != HOST_PROVIDER).map(|mut r| {
                r.hosts = vec![host.clone()];
                r
            })),
            Err(error) => failed.push(Report {
                provider: HOST_PROVIDER.into(),
                error: Some(error),
                hosts: vec![host],
                ..Default::default()
            }),
        }
    }
    // Stable, so hosts keep their order within a provider.
    all.sort_by_key(|r| provider_rank(&r.provider));

    let mut groups: Vec<Vec<Report>> = Vec::new();
    for r in all {
        match groups.last_mut() {
            Some(g) if g[0].provider == r.provider => g.push(r),
            _ => groups.push(vec![r]),
        }
    }

    let mut merged: Vec<Report> = Vec::new();
    for group in groups {
        let any_signed_in = group.iter().any(|r| r.signed_in);
        let first = merged.len();
        for mut r in group {
            if !r.signed_in && any_signed_in {
                continue;
            }
            // Not signed in anywhere: one line for the provider, as on one machine.
            let same = merged[first..].iter_mut().find(|m| {
                (r.account.is_some() && m.account == r.account) || (!r.signed_in && !m.signed_in)
            });
            match same {
                Some(m) => {
                    let mut hosts = std::mem::take(&mut m.hosts);
                    hosts.append(&mut r.hosts);
                    if m.stale && !r.stale {
                        *m = r;
                    }
                    m.hosts = hosts;
                }
                None => merged.push(r),
            }
        }
    }
    merged.extend(failed);
    merged
}

/// The ` @host,host` tag for a report whose account is not on every
/// reachable host; empty when it is, or when only one host was read.
pub fn tag(r: &Report, reachable: &[String]) -> String {
    if r.provider == HOST_PROVIDER || reachable.len() <= 1 {
        return String::new();
    }
    if reachable.iter().all(|h| r.hosts.iter().any(|x| &x.name == h)) {
        return String::new();
    }
    let names: Vec<&str> = r.hosts.iter().map(|h| h.name.as_str()).collect();
    format!(" @{}", names.join(","))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::report::Window;

    fn host(name: &str) -> HostRef {
        HostRef { name: name.into(), t3_environment: None }
    }

    fn report(provider: &str, account: Option<&str>, used: f64) -> Report {
        Report {
            provider: provider.into(),
            account: account.map(str::to_string),
            windows: vec![Window::new("Weekly", used, None, None)],
            ..Default::default()
        }
    }

    fn signed_out(provider: &str) -> Report {
        Report { provider: provider.into(), error: Some("no login".into()), signed_in: false, ..Default::default() }
    }

    fn on(host_name: &str, reports: Vec<Report>) -> Vec<Report> {
        reports
            .into_iter()
            .map(|mut r| {
                r.hosts = vec![host(host_name)];
                r
            })
            .collect()
    }

    /// Each merged report as "Provider account@host,host".
    fn summary(reports: &[Report]) -> Vec<String> {
        reports
            .iter()
            .map(|r| {
                let hosts: Vec<&str> = r.hosts.iter().map(|h| h.name.as_str()).collect();
                format!("{} {}@{}", r.provider, r.account.as_deref().unwrap_or("-"), hosts.join(","))
            })
            .collect()
    }

    #[test]
    fn config_defaults_and_overrides() {
        let home = Path::new("/home/u");
        assert_eq!(config_path_from(None, home), PathBuf::from("/home/u/.config/ai-usage/hosts.json"));
        assert_eq!(config_path_from(Some(" "), home), PathBuf::from("/home/u/.config/ai-usage/hosts.json"));
        assert_eq!(config_path_from(Some("/xdg"), home), PathBuf::from("/xdg/ai-usage/hosts.json"));

        let none = parse_config(None, "box").unwrap();
        assert_eq!(none, Config { me: host("box"), hosts: vec![] });

        let text = r#"{"self": {"name": "mac", "t3_environment": "MacBook Pro"},
            "hosts": [{"name": "rpi5", "t3_environment": "rpi5"},
                      {"name": "gpu", "ssh": "gpu.lan", "command": "~/bin/ai-usage"}]}"#;
        let c = parse_config(Some(text), "box").unwrap();
        assert_eq!(c.me, HostRef { name: "mac".into(), t3_environment: Some("MacBook Pro".into()) });
        assert_eq!(
            c.hosts,
            vec![
                Host {
                    name: "rpi5".into(),
                    ssh: "rpi5".into(),
                    command: "ai-usage".into(),
                    t3_environment: Some("rpi5".into())
                },
                Host { name: "gpu".into(), ssh: "gpu.lan".into(), command: "~/bin/ai-usage".into(), t3_environment: None },
            ]
        );
        assert_eq!(parse_config(Some(r#"{"hosts": []}"#), "box").unwrap().me, host("box"));
        assert!(parse_config(Some(r#"{"hosts": [{"nam": "x"}]}"#), "box").is_err());
        assert!(parse_config(Some(r#"{"hosts": [{"name": "box"}]}"#), "box").is_err());
        let bad_dest = parse_config(Some(r#"{"hosts": [{"name": "x", "ssh": "-oProxyCommand=evil"}]}"#), "box");
        assert!(bad_dest.unwrap_err().contains("starts with '-'"));
        assert!(parse_config(Some(r#"{"hosts": [{"name": "-x"}]}"#), "box").is_err(), "name is the default destination");
        assert!(parse_config(Some(r#"{"hosts": [{"name": "x", "command": "ai-usage\nrm -rf ~"}]}"#), "box").is_err());
    }

    #[test]
    fn same_account_collapses_and_different_accounts_stay_apart() {
        let mac = on("mac", vec![report("Claude", Some("c1"), 10.0), report("Antigravity", Some("g1"), 20.0)]);
        let pi = vec![report("Claude", Some("c1"), 99.0), report("Antigravity", Some("g2"), 30.0)];
        let merged = merge(mac, vec![(host("rpi5"), Ok(pi))]);
        assert_eq!(summary(&merged), ["Claude c1@mac,rpi5", "Antigravity g1@mac", "Antigravity g2@rpi5"]);
        assert_eq!(merged[0].windows[0].used_percent, 10.0, "the local reading wins");
    }

    #[test]
    fn a_fresh_reading_beats_a_stale_one_of_the_same_account() {
        let mut stale = report("Claude", Some("c1"), 10.0);
        stale.stale = true;
        let pi = vec![report("Claude", Some("c1"), 40.0)];
        let merged = merge(on("mac", vec![stale]), vec![(host("rpi5"), Ok(pi))]);
        assert_eq!(summary(&merged), ["Claude c1@mac,rpi5"], "hosts stay in order");
        assert_eq!(merged[0].windows[0].used_percent, 40.0);
        assert!(!merged[0].stale);

        // Both stale: the local one stays.
        let (mut a, mut b) = (report("Claude", Some("c1"), 10.0), report("Claude", Some("c1"), 40.0));
        (a.stale, b.stale) = (true, true);
        let merged = merge(on("mac", vec![a]), vec![(host("rpi5"), Ok(vec![b]))]);
        assert_eq!(merged[0].windows[0].used_percent, 10.0);
    }

    #[test]
    fn remote_reading_used_when_local_has_none() {
        let pi = vec![report("Codex", Some("x1"), 5.0)];
        let gpu = vec![report("Codex", Some("x1"), 50.0)];
        let merged = merge(vec![], vec![(host("rpi5"), Ok(pi)), (host("gpu"), Ok(gpu))]);
        assert_eq!(summary(&merged), ["Codex x1@rpi5,gpu"]);
        assert_eq!(merged[0].windows[0].used_percent, 5.0);
    }

    #[test]
    fn unknown_accounts_never_merge() {
        let mac = on("mac", vec![report("ChatLLM", None, 1.0)]);
        let pi = vec![report("ChatLLM", None, 1.0)];
        assert_eq!(summary(&merge(mac, vec![(host("rpi5"), Ok(pi))])), ["ChatLLM -@mac", "ChatLLM -@rpi5"]);
    }

    #[test]
    fn not_signed_in_is_dropped_when_another_host_has_the_provider() {
        let mac = on("mac", vec![report("Zed", Some("z1"), 1.0), signed_out("ChatLLM")]);
        let pi = vec![signed_out("Zed"), signed_out("ChatLLM")];
        let merged = merge(mac, vec![(host("rpi5"), Ok(pi))]);
        // Signed out everywhere: one entry for both, as on a single machine.
        assert_eq!(summary(&merged), ["ChatLLM -@mac,rpi5", "Zed z1@mac"]);

        // A real failure is kept per host.
        let mac = on("mac", vec![report("Claude", Some("c1"), 1.0)]);
        let mut failed = report("Claude", None, 0.0);
        failed.error = Some("HTTP 500".into());
        let merged = merge(mac, vec![(host("rpi5"), Ok(vec![failed]))]);
        assert_eq!(summary(&merged), ["Claude c1@mac", "Claude -@rpi5"]);
    }

    #[test]
    fn unreadable_host_becomes_a_host_element() {
        let mac = on("mac", vec![report("Claude", Some("c1"), 1.0)]);
        let merged = merge(mac, vec![(host("rpi5"), Err("unreachable (timeout)".into()))]);
        assert_eq!(summary(&merged), ["Claude c1@mac", "Host -@rpi5"]);
        assert_eq!(merged[1].error.as_deref(), Some("unreachable (timeout)"));
        assert!(merged[1].windows.is_empty());
    }

    #[test]
    fn tag_only_when_not_on_every_reachable_host() {
        let reachable = vec!["mac".to_string(), "rpi5".to_string()];
        let mut r = report("Claude", Some("c1"), 1.0);
        r.hosts = vec![host("mac"), host("rpi5")];
        assert_eq!(tag(&r, &reachable), "");
        r.hosts = vec![host("rpi5")];
        assert_eq!(tag(&r, &reachable), " @rpi5");
        assert_eq!(tag(&r, &reachable[1..]), "", "one reachable host: never tagged");
    }

    #[test]
    fn host_errors_from_remote_results() {
        let refused = "connect to host rpi5 port 22: Operation timed out\n";
        assert_eq!(reports_from(Some(255), "", refused).unwrap_err(), format!("unreachable (ssh: {})", refused.trim()));
        let unknown = "ssh: Could not resolve hostname nope: not known\n";
        assert_eq!(reports_from(Some(255), "", unknown).unwrap_err(), "unreachable (ssh: Could not resolve hostname nope: not known)");
        let old = "ai-usage: unknown option --local\n\nUsage: ai-usage [options]\n";
        assert_eq!(reports_from(Some(2), "", old).unwrap_err(), "ai-usage too old, update it there");
        assert_eq!(reports_from(Some(127), "", "bash: ai-usage: command not found").unwrap_err(), "ai-usage not found there");
        assert!(reports_from(Some(0), "not json", "").unwrap_err().starts_with("unreadable"));
        let json = r#"[{"provider":"Claude","windows":[],"account":"ab"}]"#;
        let ok = reports_from(Some(0), json, "").unwrap();
        assert_eq!(ok[0].account.as_deref(), Some("ab"));
        // Output is not enough: a non-zero exit or a signal is a failure.
        assert_eq!(reports_from(Some(1), json, "").unwrap_err(), "ai-usage failed there: exit 1");
        assert_eq!(reports_from(None, json, "").unwrap_err(), "ssh was killed by a signal");
    }
}
