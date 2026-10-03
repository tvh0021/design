# 2. Compact City Dataset Format for Static Distribution

Date: 2026-09-29

## Status

Accepted

## Context

The raw `worldcities.csv` contains 42,906 entries with 11 columns (4.5 MB uncompressed). Shipping 4.5 MB on GitHub Pages creates perceptible latency on mobile networks and wastes memory. The user requested supporting the top 10,000 cities ordered by population while reducing column payload to keep the application lightweight.

Options considered:
1. Load full 4.5 MB CSV and parse with PapaParse in-browser
2. Embed an SQLite database via WASM (sql.js)
3. Pre-process the top 10,000 cities by population into a compact JSON array of flat tuples `[name, country, lat, lng]`

## Decision

Pre-process `worldcities.csv` into the top 10,000 city records by population, then append the curated records in `curated_locations.json`. The runtime payload (`cities10k.json`) uses 4-element arrays: `[name, country, lat, lng]`.

## Consequences

- **Positive**: The original 10,000 city records retain their population order, and 111 selected geographic and heritage locations are searchable in the same directory.
- **Negative**: Omits administrative subdivisions and raw population counts from the runtime display.
- **Trade-off**: The runtime directory contains 10,111 records. The filename `cities10k.json` retains its original name for compatibility.
