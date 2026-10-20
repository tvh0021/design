from __future__ import annotations

from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scix_arxiv_radar.models import Paper
from scix_arxiv_radar.profiles import PROFILES
from scix_arxiv_radar.radar import Radar
from scix_arxiv_radar.sources import AdsSource, ArxivSource, SourceError


NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class FakeSource:
    def __init__(self, name: str, papers: list[Paper]) -> None:
        self.name = name
        self.papers = papers

    def fetch_profile(self, profile, since, limit):
        return self.papers[:limit]

    def search(self, query, limit):
        return self.papers[:limit]


class FailingSource:
    name = "ads"

    def fetch_profile(self, profile, since, limit):
        raise SourceError("ADS request failed with HTTP 503")

    def search(self, query, limit):
        raise SourceError("ADS request failed with HTTP 503")


def paper(source: str, *, arxiv_id: str | None = "2609.00001", doi: str | None = None) -> Paper:
    return Paper(
        title="A resistive GRMHD test", abstract="A test abstract", authors=("A. Researcher",),
        published_at=datetime(2026, 9, 10, tzinfo=UTC), updated_at=datetime(2026, 9, 10, tzinfo=UTC),
        source=source, url=f"https://example.test/{source}", arxiv_id=arxiv_id, doi=doi,
    )


class RadarTests(unittest.TestCase):
    def make_radar(self, directory: str, sources):
        return Radar(Path(directory) / "radar.sqlite", sources=sources, now=lambda: NOW)

    def test_scan_defaults_to_two_week_window_and_persists_new_papers(self):
        with TemporaryDirectory() as directory:
            radar = self.make_radar(directory, [FakeSource("arxiv", [paper("arxiv")])])
            result = radar.scan_new_papers("grmhd")
            self.assertEqual(result["scan_status"], "success")
            self.assertEqual(result["new_count"], 1)
            self.assertEqual(result["new_papers"][0]["radar_status"], "new")
            self.assertEqual(result["since"], "2026-08-28T12:00:00+00:00")
            self.assertEqual(radar.get_paper("2609.00001")["title"], "A resistive GRMHD test")

    def test_deduplicates_cross_source_by_doi(self):
        with TemporaryDirectory() as directory:
            sources = [
                FakeSource("arxiv", [paper("arxiv", doi="10.1000/example")]),
                FakeSource("ads", [paper("ads", doi="https://doi.org/10.1000/example")]),
            ]
            radar = self.make_radar(directory, sources)
            result = radar.scan_new_papers("grmhd", since="2026-09-01")
            self.assertEqual(result["new_count"], 1)
            self.assertEqual(len(result["papers"]), 1)
            self.assertEqual(result["papers"][0]["identifiers"]["doi"], "10.1000/example")

    def test_second_successful_scan_uses_previous_completion_as_watermark(self):
        with TemporaryDirectory() as directory:
            radar = self.make_radar(directory, [FakeSource("arxiv", [paper("arxiv")])])
            radar.scan_new_papers("m87", since="2026-09-01")
            result = radar.scan_new_papers("m87")
            self.assertEqual(result["since"], NOW.isoformat())
            self.assertEqual(result["new_count"], 0)

    def test_keeps_revisions_out_of_the_new_papers_list(self):
        with TemporaryDirectory() as directory:
            original = paper("arxiv")
            revision = Paper(
                title=original.title, abstract=original.abstract, authors=original.authors,
                published_at=original.published_at, updated_at=NOW, source="arxiv", url=original.url,
                arxiv_id=original.arxiv_id,
            )
            radar = self.make_radar(directory, [FakeSource("arxiv", [original])])
            radar.scan_new_papers("grmhd", since="2026-09-01")
            radar.sources = [FakeSource("arxiv", [revision])]
            result = radar.scan_new_papers("grmhd", since="2026-09-01")
            self.assertEqual(result["new_count"], 0)
            self.assertEqual(result["revision_count"], 1)
            self.assertEqual(result["revisions"][0]["radar_status"], "revision")

    def test_rejects_unknown_profile(self):
        with TemporaryDirectory() as directory:
            radar = self.make_radar(directory, [FakeSource("arxiv", [])])
            with self.assertRaisesRegex(ValueError, "Unknown profile"):
                radar.scan_new_papers("plasma")

    def test_failed_source_does_not_advance_profile_watermark(self):
        with TemporaryDirectory() as directory:
            radar = self.make_radar(
                directory, [FakeSource("arxiv", [paper("arxiv")]), FailingSource()]
            )
            result = radar.scan_new_papers("m87", since="2026-09-01")
            self.assertEqual(result["scan_status"], "failed")
            self.assertIsNone(radar.store.last_successful_scan("m87"))

    def test_parses_atom_entry_and_normalizes_arxiv_identifier(self):
        body = b'''<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>https://arxiv.org/abs/2609.00001v2</id>
            <title> A  test paper </title><summary> First\n abstract </summary>
            <published>2026-09-10T12:00:00Z</published><updated>2026-09-11T12:00:00Z</updated>
            <author><name>A. Researcher</name></author><category term="astro-ph.HE"/>
            <arxiv:doi>10.1000/EXAMPLE</arxiv:doi>
          </entry>
        </feed>'''
        papers = ArxivSource()._parse_feed(body)
        self.assertEqual(papers[0].arxiv_id, "2609.00001")
        self.assertEqual(papers[0].doi, "10.1000/example")
        self.assertEqual(papers[0].updated_at, NOW)

    def test_arxiv_free_text_search_treats_words_as_terms(self):
        source = ArxivSource()
        captured = {}
        source._query = lambda query, limit: captured.update(query=query, limit=limit) or []

        source.search('resistive GRMHD IMEX', 12)

        self.assertEqual(
            captured,
            {
                "query": 'all:"resistive" AND all:"GRMHD" AND all:"IMEX"',
                "limit": 12,
            },
        )

    def test_free_text_search_preserves_explicit_phrases(self):
        source = ArxivSource()
        captured = {}
        source._query = lambda query, limit: captured.update(query=query, limit=limit) or []

        source.search('"resistive GRMHD" IMEX', 8)

        self.assertEqual(
            captured,
            {
                "query": 'all:"resistive GRMHD" AND all:"IMEX"',
                "limit": 8,
            },
        )

    def test_ads_free_text_search_uses_abstract_title_keywords_field(self):
        source = AdsSource("test-token")
        captured = {}
        source._query = (
            lambda query, limit, sort:
            captured.update(query=query, limit=limit, sort=sort) or []
        )

        source.search('resistive GRMHD', 6)

        self.assertEqual(
            captured,
            {
                "query": 'abs:"resistive" AND abs:"GRMHD"',
                "limit": 6,
                "sort": "date desc",
            },
        )

    def test_grmhd_profile_does_not_match_unrelated_imex_papers(self):
        profile = PROFILES["grmhd"]
        self.assertNotIn("OR all:IMEX OR", profile.arxiv_query)
        self.assertNotIn("OR abs:IMEX OR", profile.ads_query)
        self.assertIn("all:IMEX AND", profile.arxiv_query)
        self.assertIn("abs:IMEX AND", profile.ads_query)

    def test_store_can_be_used_from_mcp_worker_thread(self):
        with TemporaryDirectory() as directory:
            radar = self.make_radar(directory, [FakeSource("arxiv", [paper("arxiv")])])
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(
                    radar.scan_new_papers, "grmhd", "2026-09-01", 10
                ).result()
            self.assertEqual(result["scan_status"], "success")
            self.assertEqual(result["new_count"], 1)


if __name__ == "__main__":
    unittest.main()
