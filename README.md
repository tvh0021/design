# design

Small, standalone projects that I built. The first two are live at
[tvh0021.github.io/design](https://tvh0021.github.io/design/); the rest are
command-line tools in `cli/`.

## Geodesic flight visualizer

Browser app in `maps/`. Pick two cities and it draws the shortest path between
them on an orthographic globe, with the distance, the spherical midpoint, and
an estimated flight time. Plain JavaScript, no build step. The city list is the
10,000 most populous cities.

Live at [/design/map/](https://tvh0021.github.io/design/map/).
Design notes are in `maps/docs/`.

```sh
npm start   # serve locally
npm test    # run the test suite
```

## usage-habit-report

Rust CLI in `usage-habit-report/`. It reads the local logs of Claude Code,
Codex and Antigravity into SQLite and reports token usage, estimated API cost,
model mix, task labels, subagent fan-out, and active time with idle gaps
removed. No prompt or response text is stored. `serve` opens a local dashboard
and regenerates the public snapshot in `usage-habit-report/site/`, which the
Pages deploy publishes to [/design/usage/](https://tvh0021.github.io/design/usage/).
Project names in the public snapshot are pseudonyms unless the project is a
public GitHub repo.

```sh
cd usage-habit-report
cargo build --release
./target/release/usage-habit-report --help
```

See `usage-habit-report/README.md` for the full command reference.

## cli/ai-usage

Rust CLI that prints the remaining quota on my AI subscriptions: Claude,
Codex, Antigravity, ChatLLM and Zed. It reads the credentials each client
already stores locally and never refreshes an OAuth login.

```sh
cd cli/ai-usage
cargo install --path .
ai-usage              # all providers
ai-usage claude       # one provider
ai-usage --compact    # one line
ai-usage --json
```

## cli/scix_arxiv_radar

Python MCP server that triages new arXiv and ADS/SciX papers against a few
saved research profiles and keeps a local SQLite record of what it has seen.
Read-only; arXiv needs no credentials and ADS takes a token from the
environment. See `cli/scix_arxiv_radar/README.md` for setup.
