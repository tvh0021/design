#!/usr/bin/env python3
"""
Pre-processes the top 10,000 cities by population from worldcities.csv
into a compact JSON array of the top cities plus curated location records:
[[name, country, lat, lng], ...]
"""

import csv
import json
import os
import sys

SOURCE_CSV = "/Volumes/T7/MacbookProM1/Documents/Data_Science/worldcities.csv"
OUTPUT_JSON = os.path.join(os.path.dirname(__file__), "..", "data", "cities10k.json")
CURATED_JSON = os.path.join(os.path.dirname(__file__), "..", "data", "curated_locations.json")


def build_cities(source_path=SOURCE_CSV, dest_path=OUTPUT_JSON, top_n=10000, curated_path=CURATED_JSON):
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source file not found at {source_path}")

    print(f"Reading from {source_path}...")
    rows = []
    with open(source_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            raw_pop = r.get("population", "").strip()
            try:
                pop = float(raw_pop) if raw_pop else 0.0
            except ValueError:
                pop = 0.0
            rows.append((pop, r))

    # Sort descending by population
    rows.sort(key=lambda x: x[0], reverse=True)
    top_rows = rows[:top_n]

    data = []
    for pop, r in top_rows:
        name = r.get("city_ascii", "").strip() or r.get("city", "").strip()
        country = r.get("country", "").strip()
        lat = round(float(r["lat"]), 4)
        lng = round(float(r["lng"]), 4)
        data.append([name, country, lat, lng])

    with open(curated_path, mode="r", encoding="utf-8") as f:
        curated = json.load(f)
    if not isinstance(curated, list) or any(
        not isinstance(row, list) or len(row) != 4 for row in curated
    ):
        raise ValueError(f"Curated locations must use [name, country, lat, lng] rows: {curated_path}")
    data.extend(curated)

    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    with open(dest_path, mode="w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)

    file_size_bytes = os.path.getsize(dest_path)
    print(f"Generated {dest_path}")
    print(f"Total records: {len(data)} ({len(top_rows)} cities + {len(curated)} curated locations)")
    print(f"File size: {file_size_bytes} bytes ({file_size_bytes / 1024:.2f} KB)")


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else SOURCE_CSV
    dst = sys.argv[2] if len(sys.argv) > 2 else OUTPUT_JSON
    build_cities(src, dst)
