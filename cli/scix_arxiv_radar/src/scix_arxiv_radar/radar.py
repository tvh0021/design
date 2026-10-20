"""The deep module behind the three MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from .models import Paper
from .profiles import PROFILES, Profile, require_profile
from .sources import AdsSource, ArxivSource, PaperSource, SourceError
from .store import RadarStore


class Radar:
    """Search, normalize, deduplicate, and persist papers behind a small interface."""

    def __init__(
        self,
        database_path: Path,
        sources: Iterable[PaperSource] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = RadarStore(database_path)
        self.sources = list(sources) if sources is not None else self._default_sources()
        self.now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _default_sources() -> list[PaperSource]:
        sources: list[PaperSource] = [ArxivSource()]
        if ads := AdsSource.from_environment():
            sources.append(ads)
        return sources

    def scan_new_papers(
        self, profile_name: str, since: str | None = None, limit: int = 30
    ) -> dict[str, object]:
        profile = require_profile(profile_name)
        limit = self._limit(limit)
        started = self.now().astimezone(UTC)
        lower = self._since(profile, since, started)
        scan_id = self.store.begin_scan(profile.name, lower, started)
        papers: list[Paper] = []
        source_status: dict[str, str] = {}
        for source in self.sources:
            try:
                papers.extend(source.fetch_profile(profile, lower, limit))
                source_status[source.name] = "ok"
            except SourceError as error:
                source_status[source.name] = str(error)
        result, new_count, revision_count = self._save_deduplicated(papers, started)
        complete = bool(self.sources) and all(state == "ok" for state in source_status.values())
        finished = self.now().astimezone(UTC)
        status = "success" if complete else "failed"
        details = {"sources": source_status, "paper_count": len(result)}
        self.store.finish_scan(scan_id, status, details, finished)
        return {
            "profile": profile.name,
            "profile_description": profile.description,
            "since": lower.isoformat(),
            "finished_at": finished.isoformat(),
            "scan_status": status,
            "source_status": source_status,
            "ads_enabled": any(source.name == "ads" for source in self.sources),
            **self._result_groups(result, new_count, revision_count),
        }

    def search_papers(self, query: str, source: str = "all", limit: int = 20) -> dict[str, object]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = self._limit(limit)
        chosen = self.sources if source == "all" else [item for item in self.sources if item.name == source]
        if not chosen:
            names = ", ".join(item.name for item in self.sources) or "none"
            raise ValueError(f"Source {source!r} is unavailable. Enabled sources: {names}.")
        papers: list[Paper] = []
        source_status: dict[str, str] = {}
        for item in chosen:
            try:
                papers.extend(item.search(query, limit))
                source_status[item.name] = "ok"
            except SourceError as error:
                source_status[item.name] = str(error)
        stored, new_count, revision_count = self._save_deduplicated(papers, self.now().astimezone(UTC))
        return {
            "query": query,
            "source_status": source_status,
            **self._result_groups(stored, new_count, revision_count),
        }

    def get_paper(self, identifier: str) -> dict[str, object]:
        paper = self.store.get(identifier.strip())
        if paper is None:
            raise ValueError(f"No stored paper matches {identifier!r}. Search or scan first.")
        return paper

    @staticmethod
    def available_profiles() -> dict[str, str]:
        return {name: profile.description for name, profile in PROFILES.items()}

    @staticmethod
    def _limit(value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("limit must be between 1 and 100")
        return value

    def _since(self, profile: Profile, explicit: str | None, now: datetime) -> datetime:
        if explicit:
            try:
                parsed = datetime.fromisoformat(explicit.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("since must use ISO 8601, for example 2026-08-28 or 2026-08-28T12:00:00Z") from error
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return self.store.last_successful_scan(profile.name) or (now - timedelta(days=14))

    def _save_deduplicated(
        self, papers: list[Paper], seen_at: datetime
    ) -> tuple[list[tuple[Paper, bool]], int, int]:
        merged = self._deduplicate(papers)
        saved = [self.store.upsert(paper, seen_at) for paper in merged]
        new_count = sum(1 for _, is_new in saved if is_new)
        revision_count = sum(
            1
            for paper, is_new in saved
            if not is_new and paper.updated_at and paper.published_at and paper.updated_at > paper.published_at
        )
        saved.sort(key=lambda item: item[0].published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return saved, new_count, revision_count

    @staticmethod
    def _result_groups(
        results: list[tuple[Paper, bool]], new_count: int, revision_count: int
    ) -> dict[str, object]:
        def document(paper: Paper, status: str) -> dict[str, object]:
            return {**paper.as_dict(), "radar_status": status}

        def is_revision(paper: Paper, is_new: bool) -> bool:
            return bool(
                not is_new
                and paper.updated_at
                and paper.published_at
                and paper.updated_at > paper.published_at
            )

        new = [document(paper, "new") for paper, is_new in results if is_new]
        revisions = [
            document(paper, "revision")
            for paper, is_new in results
            if is_revision(paper, is_new)
        ]
        seen = [
            document(paper, "seen")
            for paper, is_new in results
            if not is_new and not is_revision(paper, is_new)
        ]
        # `papers` keeps the MCP response easy to consume. The split lists keep
        # revisions distinct in the digest, rather than silently mixing them in.
        return {
            "new_count": new_count,
            "revision_count": revision_count,
            "new_papers": new,
            "revisions": revisions,
            "seen_papers": seen,
            "papers": new + revisions + seen,
        }

    @staticmethod
    def _deduplicate(papers: list[Paper]) -> list[Paper]:
        groups: list[list[Paper]] = []
        index: dict[tuple[str, str], int] = {}
        for paper in papers:
            matches = {index[(kind, value)] for kind, value in paper.identifiers.items() if (kind, value) in index}
            if matches:
                position = min(matches)
                groups[position].append(paper)
            else:
                position = len(groups)
                groups.append([paper])
            for kind, value in paper.identifiers.items():
                index[(kind, value)] = position
        return [Radar._merge_group(group) for group in groups]

    @staticmethod
    def _merge_group(group: list[Paper]) -> Paper:
        preferred = next((paper for paper in group if paper.source == "ads"), group[0])
        values = {kind: next((paper.identifiers[kind] for paper in group if kind in paper.identifiers), None) for kind in ("arxiv", "doi", "bibcode")}
        return Paper(
            title=preferred.title,
            abstract=preferred.abstract or next((paper.abstract for paper in group if paper.abstract), ""),
            authors=preferred.authors,
            published_at=min((paper.published_at for paper in group if paper.published_at), default=None),
            updated_at=max((paper.updated_at for paper in group if paper.updated_at), default=None),
            source=preferred.source,
            url=preferred.url,
            arxiv_id=values["arxiv"], doi=values["doi"], bibcode=values["bibcode"],
            categories=preferred.categories, journal_ref=preferred.journal_ref,
            source_record={"sources": sorted({paper.source for paper in group})},
        )
