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

Pre-process `worldcities.csv` into a streamlined JSON payload (`cities10k.json`) containing the top 10,000 cities by population formatted as an array of 4-element arrays: `[city_ascii, country, round(lat, 4), round(lng, 4)]`.

## Consequences

- **Positive**: Total uncompressed JSON payload is ~220 KB (~70 KB gzipped), parsing takes under 5 milliseconds on page load, and search autocomplete is instant across all 10,000 locations.
- **Negative**: Omits administrative subdivisions and raw population counts from the runtime display.
- **Trade-off**: The user gets access to 10,000 global cities with sub-second page initialization.
