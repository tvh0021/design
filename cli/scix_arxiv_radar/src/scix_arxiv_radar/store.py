"""SQLite persistence for scan watermarks and normalized paper identities."""

from __future__ import annotations

import json
import sqlite3
from threading import RLock
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Paper, canonical_identifier


class RadarStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        self.connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS papers (
                paper_id TEXT PRIMARY KEY,
                document_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_identifiers (
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                paper_id TEXT NOT NULL REFERENCES papers(paper_id),
                PRIMARY KEY(kind, value)
            );
            CREATE TABLE IF NOT EXISTS scans (
                scan_id INTEGER PRIMARY KEY,
                profile TEXT NOT NULL,
                since_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def last_successful_scan(self, profile: str) -> datetime | None:
        with self._lock:
            row = self.connection.execute(
                """SELECT finished_at FROM scans
                   WHERE profile = ? AND status = 'success'
                   ORDER BY scan_id DESC LIMIT 1""",
                (profile,),
            ).fetchone()
        return self._read_time(row["finished_at"]) if row else None

    def begin_scan(self, profile: str, since: datetime, started: datetime) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """INSERT INTO scans(profile, since_at, started_at, status, details_json)
                   VALUES (?, ?, ?, 'running', '{}')""",
                (profile, self._time(since), self._time(started)),
            )
            self.connection.commit()
        return int(cursor.lastrowid)

    def finish_scan(self, scan_id: int, status: str, details: dict[str, Any], finished: datetime) -> None:
        with self._lock:
            self.connection.execute(
                """UPDATE scans SET finished_at = ?, status = ?, details_json = ?
                   WHERE scan_id = ?""",
                (self._time(finished), status, json.dumps(details, sort_keys=True), scan_id),
            )
            self.connection.commit()

    def upsert(self, paper: Paper, seen_at: datetime) -> tuple[Paper, bool]:
        with self._lock:
            paper_id = self._matched_paper_id(paper) or paper.stable_id
            row = self.connection.execute(
                "SELECT document_json FROM papers WHERE paper_id = ?", (paper_id,)
            ).fetchone()
            is_new = row is None
            document = paper.as_dict()
            if row:
                document = self._merge(json.loads(row["document_json"]), document)
            self.connection.execute(
                """INSERT INTO papers(paper_id, document_json, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(paper_id) DO UPDATE SET
                     document_json = excluded.document_json,
                     last_seen_at = excluded.last_seen_at""",
                (paper_id, json.dumps(document, sort_keys=True), self._time(seen_at), self._time(seen_at)),
            )
            for kind, value in paper.identifiers.items():
                self.connection.execute(
                    """INSERT INTO paper_identifiers(kind, value, paper_id) VALUES (?, ?, ?)
                       ON CONFLICT(kind, value) DO UPDATE SET paper_id = excluded.paper_id""",
                    (kind, value, paper_id),
                )
            self.connection.commit()
        return self._paper_from_document(document), is_new

    def get(self, identifier: str) -> dict[str, Any] | None:
        with self._lock:
            kind, separator, value = identifier.partition(":")
            if separator and kind in {"arxiv", "doi", "bibcode"}:
                value = canonical_identifier(kind, value) or ""
                row = self.connection.execute(
                    """SELECT p.document_json FROM papers p JOIN paper_identifiers i
                       ON p.paper_id = i.paper_id WHERE i.kind = ? AND i.value = ?""",
                    (kind, value),
                ).fetchone()
            else:
                row = self.connection.execute(
                    "SELECT document_json FROM papers WHERE paper_id = ?", (identifier,)
                ).fetchone()
                if row is None:
                    row = self.connection.execute(
                        """SELECT p.document_json FROM papers p JOIN paper_identifiers i
                           ON p.paper_id = i.paper_id WHERE i.value = ?""",
                        (identifier,),
                    ).fetchone()
        return json.loads(row["document_json"]) if row else None

    def _matched_paper_id(self, paper: Paper) -> str | None:
        for kind, value in paper.identifiers.items():
            row = self.connection.execute(
                "SELECT paper_id FROM paper_identifiers WHERE kind = ? AND value = ?",
                (kind, value),
            ).fetchone()
            if row:
                return str(row["paper_id"])
        return None

    @staticmethod
    def _merge(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = dict(old)
        for key, value in new.items():
            if value not in (None, "", [], {}, ()):
                merged[key] = value
        identifiers = dict(old.get("identifiers", {}))
        identifiers.update(new.get("identifiers", {}))
        merged["identifiers"] = identifiers
        return merged

    @staticmethod
    def _paper_from_document(document: dict[str, Any]) -> Paper:
        return Paper(
            title=document["title"], abstract=document["abstract"], authors=tuple(document["authors"]),
            published_at=RadarStore._read_time(document.get("published_at")),
            updated_at=RadarStore._read_time(document.get("updated_at")), source=document["source"],
            url=document["url"], arxiv_id=document.get("arxiv_id"), doi=document.get("doi"),
            bibcode=document.get("bibcode"), categories=tuple(document.get("categories", [])),
            journal_ref=document.get("journal_ref"), source_record=document.get("source_record", {}),
        )

    @staticmethod
    def _time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _read_time(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value).astimezone(UTC) if value else None
