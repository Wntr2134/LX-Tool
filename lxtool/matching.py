"""Fixture matching and change planning.

Two questions, both asked by a tech holding a fixture the desk doesn't know:

1. *Do I already have this?*  -> :func:`find_candidates` ranks the existing
   library by how close each entry is.
2. *If I use the closest one, what do I have to change?*  -> :func:`plan`
   produces an ordered list of edits against a specific mode.

Scoring is deliberately explainable.  A tech has to trust the answer at 2am
with a show in an hour, so every score comes with the reasons behind it
rather than a single opaque number.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from . import attributes
from .model import Channel, Fixture, Mode

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    return _NON_ALNUM.sub("", s.lower())


def name_similarity(a: str, b: str) -> float:
    """0..1 similarity between two manufacturer/model strings."""
    sa, sb = _slug(a), _slug(b)
    if not sa or not sb:
        return 0.0
    if sa == sb:
        return 1.0
    if sa in sb or sb in sa:
        return 0.9
    return difflib.SequenceMatcher(None, sa, sb).ratio()


@dataclass
class Edit:
    """One change a tech has to make to turn a candidate into the target."""

    action: str          # 'add' | 'remove' | 'move' | 'retype' | 'resolution'
    offset: int
    attribute: str
    detail: str
    severity: int        # 1 (cosmetic) .. 5 (fixture won't work)

    def __str__(self) -> str:
        return f"ch {self.offset:>3}  {self.action:<10} {self.attribute:<14} {self.detail}"


@dataclass
class Match:
    """A candidate library entry scored against the target fixture."""

    fixture: Fixture
    mode: Mode
    score: float
    reasons: list[str] = field(default_factory=list)
    edits: list[Edit] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        return self.score >= 0.999 and not self.edits

    @property
    def label(self) -> str:
        return f"{self.fixture.key} [{self.mode.name}]"


def _footprint(mode: Mode) -> int:
    """Declared channel count, falling back to a count parsed from a filename."""
    declared = mode.__dict__.get("_declared_count")
    if declared:
        return int(declared)
    return mode.channel_count


def compare_modes(target: Mode, candidate: Mode) -> tuple[float, list[Edit]]:
    """Score ``candidate`` against ``target`` and list the edits to reconcile them.

    Returns a score in 0..1 and the ordered edit list.  When the candidate has
    no channel detail - which is the case for ChamSys heads until a ``.hed``
    decoder exists - only the footprint can be compared, and the score is
    capped to reflect that uncertainty.
    """
    t_by_off = target.by_offset()
    c_by_off = candidate.by_offset()

    if not c_by_off:
        # Footprint-only comparison.
        t_size, c_size = _footprint(target), _footprint(candidate)
        if not t_size or not c_size:
            return 0.0, []
        if t_size == c_size:
            # Same size is meaningful but far from proof of a match.
            return 0.55, []
        spread = abs(t_size - c_size) / max(t_size, c_size)
        return max(0.0, 0.5 - spread), []

    edits: list[Edit] = []
    offsets = sorted(set(t_by_off) | set(c_by_off))
    matched = 0

    # Where an attribute exists in both but at different offsets, that is a
    # move rather than an add+remove - much cheaper for the tech to action.
    t_attr_offsets: dict[str, list[int]] = {}
    for off, ch in t_by_off.items():
        if not ch.fine:
            t_attr_offsets.setdefault(ch.attribute, []).append(off)
    c_attr_offsets: dict[str, list[int]] = {}
    for off, ch in c_by_off.items():
        if not ch.fine:
            c_attr_offsets.setdefault(ch.attribute, []).append(off)

    moved: set[int] = set()
    for attr, t_offs in t_attr_offsets.items():
        c_offs = c_attr_offsets.get(attr, [])
        for t_off, c_off in zip(sorted(t_offs), sorted(c_offs)):
            if t_off != c_off:
                edits.append(Edit(
                    action="move",
                    offset=c_off,
                    attribute=attr,
                    detail=f"move from ch {c_off} to ch {t_off}",
                    severity=attributes.criticality(attr),
                ))
                moved.update({t_off, c_off})

    for off in offsets:
        t_ch, c_ch = t_by_off.get(off), c_by_off.get(off)

        if t_ch and c_ch:
            if t_ch.attribute == c_ch.attribute:
                matched += 1
                if t_ch.fine != c_ch.fine:
                    edits.append(Edit(
                        action="resolution",
                        offset=off,
                        attribute=t_ch.attribute,
                        detail=f"{'16-bit' if t_ch.fine else '8-bit'} expected, "
                               f"library has {'16-bit' if c_ch.fine else '8-bit'}",
                        severity=2,
                    ))
            elif off not in moved:
                edits.append(Edit(
                    action="retype",
                    offset=off,
                    attribute=t_ch.attribute,
                    detail=f"library has {c_ch.attribute} ({c_ch.name}), needs {t_ch.attribute}",
                    severity=attributes.criticality(t_ch.attribute),
                ))
        elif t_ch and not c_ch:
            if off not in moved:
                edits.append(Edit(
                    action="add",
                    offset=off,
                    attribute=t_ch.attribute,
                    detail=f"add {t_ch.attribute} ({t_ch.name})",
                    severity=attributes.criticality(t_ch.attribute),
                ))
        elif c_ch and not t_ch:
            if off not in moved:
                edits.append(Edit(
                    action="remove",
                    offset=off,
                    attribute=c_ch.attribute,
                    detail=f"remove {c_ch.attribute} ({c_ch.name})",
                    severity=max(1, attributes.criticality(c_ch.attribute) - 1),
                ))

    total = max(len(offsets), 1)
    score = matched / total
    return score, sort_edits(edits)


def sort_edits(edits: list[Edit]) -> list[Edit]:
    """Order edits the way a tech should work through them.

    Most damaging first, then in patch order so you walk the channel list once
    rather than jumping around the Head Editor.
    """
    return sorted(edits, key=lambda e: (-e.severity, e.offset))


def score_pair(target: Fixture, t_mode: Mode, cand: Fixture, c_mode: Mode) -> Match:
    """Score one target mode against one candidate mode."""
    mfr = name_similarity(target.manufacturer, cand.manufacturer)
    model = name_similarity(target.model, cand.model)
    chan_score, edits = compare_modes(t_mode, c_mode)

    # Channel layout dominates: a same-layout fixture from another brand is far
    # more useful than a same-brand fixture with a different layout.
    score = 0.60 * chan_score + 0.25 * model + 0.15 * mfr

    reasons: list[str] = []
    if mfr >= 0.9:
        reasons.append("manufacturer matches")
    if model >= 0.9:
        reasons.append("model name matches")
    t_size, c_size = _footprint(t_mode), _footprint(c_mode)
    if t_size and t_size == c_size:
        reasons.append(f"same footprint ({t_size} ch)")
    elif t_size and c_size:
        reasons.append(f"footprint differs ({c_size} ch vs {t_size} ch)")
    if not c_mode.by_offset():
        reasons.append("channel detail unavailable - compared on footprint only")
    if edits:
        worst = max(e.severity for e in edits)
        reasons.append(f"{len(edits)} change(s) needed, worst severity {worst}/5")

    return Match(fixture=cand, mode=c_mode, score=round(score, 4), reasons=reasons, edits=edits)


def find_candidates(
    target: Fixture,
    target_mode: Mode,
    library: list[Fixture],
    *,
    limit: int = 10,
    min_score: float = 0.0,
) -> list[Match]:
    """Rank every (fixture, mode) in ``library`` against the target mode."""
    matches = [
        score_pair(target, target_mode, cand, c_mode)
        for cand in library
        for c_mode in cand.modes
    ]
    matches = [m for m in matches if m.score >= min_score]
    matches.sort(key=lambda m: (-m.score, len(m.edits), m.label))
    return matches[:limit]


def plan(target: Fixture, target_mode: Mode, match: Match) -> list[Edit]:
    """The ordered edit list to turn ``match`` into ``target_mode``."""
    _, edits = compare_modes(target_mode, match.mode)
    return edits
