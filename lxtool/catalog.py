"""Open Fixture Library catalogue: fetch once, search offline.

OFL publishes its entire library as a single zip at ``/download.ofl`` holding
``<manufacturer>/<fixture>.json``.  Pulling that once and caching it locally
means fixture lookup is instant and works in a venue with no signal - which is
where you actually need it.

    from lxtool.catalog import Catalog
    Catalog.download()          # ~3 MB, once
    cat = Catalog.load()
    cat.search("mac 700")

Uses only the standard library, so the core install stays dependency-free.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .formats import ofl
from .model import Fixture

BULK_URL = "https://open-fixture-library.org/download.ofl"
MANUFACTURERS_URL = "https://open-fixture-library.org/api/v1/manufacturers"
FIXTURE_URL = "https://open-fixture-library.org/{manufacturer}/{fixture}.json"

_USER_AGENT = "LX-Tool/1.0 (+https://github.com/Wntr2134/LX-Tool)"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def cache_dir() -> Path:
    """Where the catalogue lives.  Override with ``LXTOOL_CACHE``."""
    env = os.environ.get("LXTOOL_CACHE")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "lxtool"


def _slug(s: str) -> str:
    return _NON_ALNUM.sub(" ", s.lower()).strip()


@dataclass
class Entry:
    """One fixture in the catalogue, indexed for search but not yet parsed.

    Parsing every fixture up front would be wasted work - search only needs
    the names and the mode summary.
    """

    manufacturer_key: str
    fixture_key: str
    manufacturer: str
    name: str
    categories: tuple[str, ...]
    modes: tuple[tuple[str, int], ...]      # (mode name, channel count)
    _raw: dict

    @property
    def key(self) -> str:
        return f"{self.manufacturer_key}/{self.fixture_key}"

    @property
    def label(self) -> str:
        return f"{self.manufacturer} {self.name}"

    def to_fixture(self) -> Fixture:
        fx = ofl.parse(self._raw, manufacturer=self.manufacturer)
        # OFL stores the manufacturer as a lower-case key; prefer the display
        # form so output reads "Martin MAC 700" rather than "martin MAC 700".
        fx.manufacturer = self.manufacturer
        fx.source_id = self.key
        return fx


class Catalog:
    """A searchable local copy of the Open Fixture Library."""

    def __init__(self, entries: list[Entry], fetched_at: float | None = None):
        self.entries = entries
        self.fetched_at = fetched_at

    def __len__(self) -> int:
        return len(self.entries)

    # -- acquisition -------------------------------------------------------

    @staticmethod
    def archive_path(directory: Path | str | None = None) -> Path:
        return Path(directory or cache_dir()) / "open-fixture-library.ofl"

    @classmethod
    def download(cls, directory: Path | str | None = None, *, timeout: int = 120) -> Path:
        """Fetch the bulk archive.  Returns the path it was written to."""
        dest = cls.archive_path(directory)
        dest.parent.mkdir(parents=True, exist_ok=True)

        from .net import urlopen

        payload = urlopen(BULK_URL, timeout=timeout)

        if not payload.startswith(b"PK"):
            raise ValueError(f"{BULK_URL} did not return a zip archive")

        tmp = dest.with_suffix(".part")
        tmp.write_bytes(payload)
        tmp.replace(dest)   # atomic, so an interrupted fetch can't corrupt the cache
        return dest

    @classmethod
    def load(cls, directory: Path | str | None = None) -> "Catalog":
        """Load the cached archive.  Raises if it hasn't been downloaded."""
        path = cls.archive_path(directory)
        if not path.exists():
            raise FileNotFoundError(
                f"no cached catalogue at {path}. Run 'lx fetch' first."
            )

        entries: list[Entry] = []
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith(".json"):
                    continue
                try:
                    doc = json.loads(zf.read(name))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(doc, dict) or "modes" not in doc:
                    continue

                mfr_key = doc.get("manufacturerKey") or name.split("/")[0]
                fx_key = doc.get("fixtureKey") or Path(name).stem
                modes = tuple(
                    (m.get("name", "?"), len(m.get("channels") or []))
                    for m in doc.get("modes", [])
                    if isinstance(m, dict)
                )
                entries.append(Entry(
                    manufacturer_key=mfr_key,
                    fixture_key=fx_key,
                    manufacturer=mfr_key.replace("-", " ").title(),
                    name=doc.get("name", fx_key),
                    categories=tuple(doc.get("categories") or []),
                    modes=modes,
                    _raw=doc,
                ))

        entries.sort(key=lambda e: (e.manufacturer_key, e.fixture_key))
        return cls(entries, fetched_at=path.stat().st_mtime)

    @property
    def age_days(self) -> float | None:
        if self.fetched_at is None:
            return None
        return (time.time() - self.fetched_at) / 86400

    # -- lookup ------------------------------------------------------------

    def get(self, key: str) -> Entry | None:
        """Fetch by ``manufacturer/fixture`` key."""
        for e in self.entries:
            if e.key == key:
                return e
        return None

    def search(self, query: str, *, limit: int = 20, channels: int | None = None) -> list[Entry]:
        """Rank the catalogue against a free-text query."""
        return [e for _, e in self.search_scored(query, limit=limit, channels=channels)]

    def search_scored(
        self, query: str, *, limit: int = 20, channels: int | None = None
    ) -> list[tuple[float, Entry]]:
        """Ranked results with their scores.

        Scores whole-word hits above substring hits so that searching "mac 700"
        puts the MAC 700 above a fixture merely containing "700".  Callers that
        need to decide whether a query resolved unambiguously use the scores.
        """
        terms = [t for t in _slug(query).split() if t]
        if not terms:
            return []

        scored: list[tuple[float, Entry]] = []
        for e in self.entries:
            hay = _slug(f"{e.manufacturer} {e.name} {' '.join(e.categories)}")
            words = set(hay.split())

            score = 0.0
            for t in terms:
                if t in words:
                    score += 1.0
                elif len(t) >= 3 and t in hay:
                    # Substring credit only for terms long enough to be
                    # meaningful; "a" or "r" would otherwise match everything.
                    score += 0.5
            if score == 0:
                continue

            score /= len(terms)
            if _slug(e.name) == _slug(query):
                score += 1.0
            if channels is not None and any(c == channels for _, c in e.modes):
                score += 0.25

            scored.append((score, e))

        scored.sort(key=lambda pair: (-pair[0], pair[1].key))
        return scored[:limit]

    def manufacturers(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            counts[e.manufacturer] = counts.get(e.manufacturer, 0) + 1
        return dict(sorted(counts.items()))


def fetch_one(manufacturer_key: str, fixture_key: str, *, timeout: int = 30) -> Fixture:
    """Fetch a single fixture live, bypassing the cache.

    Useful for something added to OFL since the last bulk download.
    """
    from .net import urlopen

    url = FIXTURE_URL.format(manufacturer=manufacturer_key, fixture=fixture_key)
    doc = json.loads(urlopen(url, timeout=timeout))
    fx = ofl.parse(doc, manufacturer=manufacturer_key)
    fx.source_id = f"{manufacturer_key}/{fixture_key}"
    return fx
