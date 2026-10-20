"""Read-only adapters for the upstream paper sources."""

from __future__ import annotations

import json
import os
import shlex
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as element_tree
from datetime import UTC, datetime
from typing import Protocol

from .models import Paper
from .profiles import Profile

USER_AGENT = "scix-arxiv-radar/0.1 (local research workflow)"
ARXIV_URL = "https://export.arxiv.org/api/query"
ADS_URL = "https://api.adsabs.harvard.edu/v1/search/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class SourceError(RuntimeError):
    """An upstream source could not provide a complete response."""


class PaperSource(Protocol):
    name: str

    def fetch_profile(self, profile: Profile, since: datetime, limit: int) -> list[Paper]: ...

    def search(self, query: str, limit: int) -> list[Paper]: ...


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if len(value) == 7 and value[4] == "-":
            value = f"{value}-01"
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def free_text_query(query: str, field: str) -> str:
    """Turn shell-like words and quoted phrases into explicit source terms."""
    try:
        raw_terms = shlex.split(query)
    except ValueError:
        raw_terms = query.replace('"', " ").split()
    terms = [
        clean_text(term.replace("\\", " ").replace('"', " "))
        for term in raw_terms
    ]
    terms = [term for term in terms if term]
    if not terms:
        raise ValueError("query must contain at least one word")
    return " AND ".join(f'{field}:"{term}"' for term in terms)


class ArxivSource:
    name = "arxiv"

    def fetch_profile(self, profile: Profile, since: datetime, limit: int) -> list[Paper]:
        until = datetime.now(UTC).strftime("%Y%m%d%H%M")
        date_range = since.astimezone(UTC).strftime("%Y%m%d%H%M")
        query = f"{profile.arxiv_query} AND submittedDate:[{date_range} TO {until}]"
        return self._query(query, limit)

    def search(self, query: str, limit: int) -> list[Paper]:
        return self._query(free_text_query(query, "all"), limit)

    def _query(self, query: str, limit: int) -> list[Paper]:
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "start": 0,
                "max_results": limit,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        request = urllib.request.Request(
            f"{ARXIV_URL}?{params}", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.URLError as error:
            raise SourceError(f"arXiv request failed: {error.reason}") from error
        return self._parse_feed(body)

    def _parse_feed(self, body: bytes) -> list[Paper]:
        try:
            root = element_tree.fromstring(body)
        except element_tree.ParseError as error:
            raise SourceError("arXiv returned malformed Atom XML") from error
        papers: list[Paper] = []
        for entry in root.findall(f"{ATOM}entry"):
            title = clean_text(entry.findtext(f"{ATOM}title"))
            if title == "Error":
                raise SourceError(clean_text(entry.findtext(f"{ATOM}summary")) or "arXiv query failed")
            url = clean_text(entry.findtext(f"{ATOM}id"))
            arxiv_id = url.rsplit("/", 1)[-1]
            doi = clean_text(entry.findtext(f"{ARXIV}doi")) or None
            papers.append(
                Paper(
                    title=title,
                    abstract=clean_text(entry.findtext(f"{ATOM}summary")),
                    authors=tuple(
                        clean_text(author.findtext(f"{ATOM}name"))
                        for author in entry.findall(f"{ATOM}author")
                    ),
                    published_at=parse_time(entry.findtext(f"{ATOM}published")),
                    updated_at=parse_time(entry.findtext(f"{ATOM}updated")),
                    source=self.name,
                    url=url,
                    arxiv_id=arxiv_id,
                    doi=doi,
                    categories=tuple(category.attrib["term"] for category in entry.findall(f"{ATOM}category")),
                    journal_ref=clean_text(entry.findtext(f"{ARXIV}journal_ref")) or None,
                )
            )
        return papers


class AdsSource:
    name = "ads"

    def __init__(self, token: str) -> None:
        self._token = token

    @classmethod
    def from_environment(cls) -> "AdsSource | None":
        token = os.environ.get("ADS_API_TOKEN", "").strip()
        return cls(token) if token else None

    def fetch_profile(self, profile: Profile, since: datetime, limit: int) -> list[Paper]:
        # ADS `pubdate` is often only month-granular. Retrieve the newest topical
        # records, then apply the local high-water mark to full timestamps where
        # ADS provides them. arXiv remains the fresh submission feed.
        papers = self._query(profile.ads_query, limit, "date desc")
        return [
            paper
            for paper in papers
            if (paper.updated_at or paper.published_at) is None
            or (paper.updated_at or paper.published_at) >= since
        ]

    def search(self, query: str, limit: int) -> list[Paper]:
        return self._query(free_text_query(query, "abs"), limit, "date desc")

    def _query(self, query: str, limit: int, sort: str) -> list[Paper]:
        params = urllib.parse.urlencode(
            {
                "q": query,
                "rows": limit,
                "sort": sort,
                "fl": "bibcode,title,abstract,author,pubdate,entry_date,doi,identifier,keyword,url",
            }
        )
        request = urllib.request.Request(
            f"{ADS_URL}?{params}",
            headers={"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise SourceError(f"ADS request failed with HTTP {error.code}") from error
        except (urllib.error.URLError, json.JSONDecodeError) as error:
            raise SourceError("ADS request failed") from error
        return [self._paper(document) for document in payload.get("response", {}).get("docs", [])]

    def _paper(self, document: dict[str, object]) -> Paper:
        identifiers = tuple(document.get("identifier", []) or [])
        arxiv_id = next(
            (value.split(":", 1)[-1] for value in identifiers if value.startswith("arXiv:")), None
        )
        dois = tuple(document.get("doi", []) or [])
        titles = tuple(document.get("title", []) or [])
        return Paper(
            title=clean_text(str(titles[0] if titles else "Untitled")),
            abstract=clean_text(str(document.get("abstract") or "")),
            authors=tuple(str(name) for name in (document.get("author", []) or [])),
            published_at=parse_time(str(document.get("pubdate") or "")),
            updated_at=parse_time(str(document.get("entry_date") or "")),
            source=self.name,
            url=f"https://ui.adsabs.harvard.edu/abs/{document['bibcode']}/abstract",
            arxiv_id=arxiv_id,
            doi=str(dois[0]) if dois else None,
            bibcode=str(document.get("bibcode") or "") or None,
            categories=tuple(str(item) for item in (document.get("keyword", []) or [])),
            source_record={"ads_url": str(document.get("url") or "")},
        )
