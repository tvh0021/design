# SciX + arXiv paper radar

This is a local, read-only MCP server for two paper-monitoring profiles:

- `grmhd`: resistive GRMHD, IMEX, constrained transport, and higher-order finite-volume methods.
- `m87`: M87, AGN feedback, turbulence, and XRISM.

It exposes three tools:

- `scan_new_papers` searches the selected profile since its last complete scan, or a supplied date. It deduplicates by arXiv ID, DOI, and ADS bibcode. It keeps revisions separate from new papers.
- `search_papers` searches live sources with a short free-text query and saves the results locally.
- `get_paper` retrieves a paper previously stored by either arXiv ID, DOI, bibcode, or internal ID.

The radar writes its SQLite database to `data/radar.sqlite` by default. Set
`PAPER_RADAR_DB` when the MCP process cannot write beside the installed package.
It does not write to ADS, arXiv, or a reference library.

## Install and run

```sh
cd /Users/tvh0021/git_repos/design/cli/scix_arxiv_radar
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

arXiv works without credentials. To enable the ADS/SciX search adapter, create an ADS token and export it only in the MCP process environment:

```sh
export ADS_API_TOKEN='your-token'
```

Start the server with:

```sh
.venv/bin/scix-arxiv-radar
```

For a Codex-style MCP configuration, use:

```json
{
  "mcpServers": {
    "scix-arxiv-radar": {
      "command": "/Users/tvh0021/git_repos/design/cli/scix_arxiv_radar/.venv/bin/scix-arxiv-radar",
      "env": {
        "ADS_API_TOKEN": "your-token",
        "PAPER_RADAR_DB": "/path/to/a/writable/radar.sqlite"
      }
    }
  }
}
```

Do not put the token in a tracked file. The server never logs request headers.

## First calls

```text
scan_new_papers(profile="grmhd", since="2026-08-28", limit=30)
scan_new_papers(profile="m87", since="2026-08-28", limit=30)
search_papers(query="resistive GRMHD IMEX", source="arxiv", limit=10)
```

Unquoted words in a free-text search are combined with `AND`. Explicit quotes
preserve phrases, so `"resistive GRMHD" IMEX` searches for that phrase plus the
term `IMEX`.

The initial `scan_new_papers` call looks back 14 days when `since` is omitted. Later calls use the completion time of the last scan where every enabled source returned successfully. If a live source fails, the radar records the failed scan and does not advance that profile's watermark.

## Design notes

The public module has only three tools. Source-specific query syntax, Atom parsing, ADS response handling, identity matching, and scan bookkeeping stay behind that interface. This keeps the MCP tools stable when an upstream source changes.

The arXiv adapter uses the documented `submittedDate` range and descending submission-date sort. The ADS adapter uses `Authorization: Bearer` only when `ADS_API_TOKEN` is present.
