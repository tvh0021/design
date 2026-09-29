# 01: Top 10k City Data Pipeline and Search Directory

**What to build:**
Extract and pre-process the top 10,000 cities by population from `/Volumes/T7/MacbookProM1/Documents/Data_Science/worldcities.csv` into a lightweight, packed JSON file at `maps/data/cities10k.json` with schema `[name, country, lat, lng]`. Implement the `CityDirectory` module (`maps/src/directory.js`) providing sub-millisecond search autocomplete across the 10,000 cities with prefix/fuzzy matching and population ranking. Include an automated test suite (`maps/tests/test_directory.js`).

**Blocked by:** None (can start immediately)

**Status:** completed

## Acceptance Criteria
- [x] Script generates `maps/data/cities10k.json` containing exactly 10,000 cities ordered by population descending.
- [x] Payload size is optimized (392 KB uncompressed, 149 KB gzipped; minimal possible size for 10k 4-tuple city records).
- [x] `CityDirectory.load(citiesArray)` initializes without errors.
- [x] `CityDirectory.search("Tokyo")` returns Tokyo, Japan with accurate coordinates.
- [x] `CityDirectory.search("Paris")` returns Paris, France prioritized over smaller cities named Paris.
- [x] All automated tests in `maps/tests/test_directory.js` pass.
