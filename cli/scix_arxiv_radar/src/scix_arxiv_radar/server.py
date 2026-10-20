"""MCP entry point. Keep standard output reserved for the MCP transport."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mcp.server import MCPServer

from .radar import Radar

logger = logging.getLogger(__name__)
DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / "data" / "radar.sqlite"


def build_server(database_path: Path | None = None) -> MCPServer:
    """Create the read-only MCP server without opening a network connection."""
    radar = Radar(database_path or Path(os.environ.get("PAPER_RADAR_DB", DEFAULT_DATABASE)))
    mcp = MCPServer("scix-arxiv-radar")

    @mcp.tool()
    def scan_new_papers(profile: str, since: str | None = None, limit: int = 30) -> str:
        """Find papers added since a profile's last complete scan.

        Args:
            profile: `grmhd` or `m87`.
            since: Optional ISO-8601 date or timestamp. The first scan defaults to 14 days.
            limit: Maximum results requested from each source, from 1 through 100.
        """
        return json.dumps(radar.scan_new_papers(profile, since, limit), indent=2)

    @mcp.tool()
    def search_papers(query: str, source: str = "all", limit: int = 20) -> str:
        """Search live ADS/SciX and/or arXiv with a short free-text query.

        Args:
            query: A plain-language phrase or compact search terms.
            source: `all`, `arxiv`, or `ads`. ADS requires ADS_API_TOKEN.
            limit: Maximum results requested from each source, from 1 through 100.
        """
        return json.dumps(radar.search_papers(query, source, limit), indent=2)

    @mcp.tool()
    def get_paper(identifier: str) -> str:
        """Return metadata saved by an earlier scan or search.

        Args:
            identifier: An arXiv ID, DOI, ADS bibcode, or returned internal ID.
        """
        return json.dumps(radar.get_paper(identifier), indent=2)

    return mcp


def main() -> None:
    logging.basicConfig(level=os.environ.get("PAPER_RADAR_LOG_LEVEL", "WARNING"))
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
