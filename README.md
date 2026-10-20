# design

Small, standalone projects that I built. Both are live at
[tvh0021.github.io/design](https://tvh0021.github.io/design/).

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
