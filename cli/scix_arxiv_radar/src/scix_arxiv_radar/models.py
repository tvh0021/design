"""Value objects crossing the radar's internal source seam."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any


def canonical_identifier(kind: str, value: str | None) -> str | None:
    """Return a stable identifier value, or None when the source did not provide one."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if kind == "doi":
        return value.removeprefix("https://doi.org/").removeprefix("doi:").lower()
    if kind == "arxiv":
        return value.removeprefix("https://arxiv.org/abs/").split("v", 1)[0]
    return value


@dataclass(frozen=True, slots=True)
class Paper:
    """Normalized paper metadata from one upstream source."""

    title: str
    abstract: str
    authors: tuple[str, ...]
    published_at: datetime | None
    updated_at: datetime | None
    source: str
    url: str
    arxiv_id: str | None = None
    doi: str | None = None
    bibcode: str | None = None
    categories: tuple[str, ...] = ()
    journal_ref: str | None = None
    source_record: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arxiv_id", canonical_identifier("arxiv", self.arxiv_id))
        object.__setattr__(self, "doi", canonical_identifier("doi", self.doi))
        object.__setattr__(self, "bibcode", canonical_identifier("bibcode", self.bibcode))

    @property
    def identifiers(self) -> dict[str, str]:
        return {
            kind: value
            for kind, value in (
                ("arxiv", self.arxiv_id),
                ("doi", self.doi),
                ("bibcode", self.bibcode),
            )
            if value
        }

    @property
    def stable_id(self) -> str:
        for kind, value in self.identifiers.items():
            return f"{kind}:{value}"
        digest = sha256(f"{self.title}|{self.url}".encode()).hexdigest()[:20]
        return f"local:{digest}"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for name in ("published_at", "updated_at"):
            if data[name] is not None:
                data[name] = data[name].isoformat()
        data["id"] = self.stable_id
        data["identifiers"] = self.identifiers
        return data
