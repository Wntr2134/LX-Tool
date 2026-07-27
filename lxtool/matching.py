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
    """DMX slots the mode occupies, declared or derived."""
    return mode.channel_count


def _sequence(mode: Mode) -> list[Channel]:
    """Channels in patch order - the sequence alignment runs over."""
    return sorted(mode.channels, key=lambda c: c.offset)


def _token(ch: Channel) -> tuple[str, bool]:
    return (ch.attribute, ch.fine)


def content_score(target: Mode, candidate: Mode) -> float:
    """How much of the *same stuff* is present, ignoring order entirely.

    Jaccard over coarse attribute sets.  Separating this from ordering matters
    because "right channels, wrong order" is a five-minute repatch while
    "missing channels" means building a head.
    """
    a, b = target.attribute_set(), candidate.attribute_set()
    a.discard("Unknown")
    b.discard("Unknown")
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compare_modes(target: Mode, candidate: Mode) -> tuple[float, list[Edit]]:
    """Score ``candidate`` against ``target`` and list the edits to reconcile them.

    Uses sequence alignment rather than slot-by-slot comparison.  That matters:
    if a fixture inserts one channel near the top, a positional comparison
    reports every later slot as wrong, while alignment correctly reports a
    single insertion and leaves the rest matched.

    Returns a score in 0..1 and the ordered edit list.  When the candidate has
    no channel detail - which is the case for ChamSys heads until a ``.hed``
    decoder exists - only the footprint can be compared, and the score is
    capped to reflect that uncertainty.
    """
    if not candidate.channels:
        # Footprint-only comparison.
        t_size, c_size = _footprint(target), _footprint(candidate)
        if not t_size or not c_size:
            return 0.0, []
        if t_size == c_size:
            # Same size is meaningful but far from proof of a match.
            return 0.55, []
        spread = abs(t_size - c_size) / max(t_size, c_size)
        return max(0.0, 0.5 - spread), []

    t_seq, c_seq = _sequence(target), _sequence(candidate)
    matcher = difflib.SequenceMatcher(
        a=[_token(c) for c in c_seq], b=[_token(c) for c in t_seq], autojunk=False
    )

    edits: list[Edit] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            # Pair them up positionally within the block; any surplus on
            # either side becomes an add or a remove.
            for k in range(max(i2 - i1, j2 - j1)):
                c_ch = c_seq[i1 + k] if i1 + k < i2 else None
                t_ch = t_seq[j1 + k] if j1 + k < j2 else None
                if c_ch is not None and t_ch is not None:
                    edits.append(_retype(t_ch, c_ch))
                elif t_ch is not None:
                    edits.append(_add(t_ch))
                elif c_ch is not None:
                    edits.append(_remove(c_ch))
        elif tag == "insert":
            for t_ch in t_seq[j1:j2]:
                edits.append(_add(t_ch))
        elif tag == "delete":
            for c_ch in c_seq[i1:i2]:
                edits.append(_remove(c_ch))

    edits = _collapse_moves(edits)

    order = matcher.ratio()
    content = content_score(target, candidate)
    # Content is weighted a little higher than ordering: having the right
    # parameters at the wrong offsets is a far smaller job than lacking them.
    score = 0.45 * order + 0.55 * content

    # A colour-system mismatch is disqualifying in practice, so it must cost
    # more than a couple of stray channels would.
    t_col = attributes.colour_system(target.attribute_set())
    c_col = attributes.colour_system(candidate.attribute_set())
    if t_col != c_col and "none" not in (t_col, c_col):
        score *= 0.6

    return round(min(score, 1.0), 4), sort_edits(edits)


def _add(t_ch: Channel) -> Edit:
    return Edit(
        action="add",
        offset=t_ch.offset,
        attribute=t_ch.attribute,
        detail=f"insert {t_ch.attribute} ({t_ch.name})",
        severity=attributes.criticality(t_ch.attribute),
    )


def _remove(c_ch: Channel) -> Edit:
    return Edit(
        action="remove",
        offset=c_ch.offset,
        attribute=c_ch.attribute,
        detail=f"remove {c_ch.attribute} ({c_ch.name})",
        severity=max(1, attributes.criticality(c_ch.attribute) - 1),
    )


def _retype(t_ch: Channel, c_ch: Channel) -> Edit:
    if t_ch.attribute == c_ch.attribute and t_ch.fine != c_ch.fine:
        return Edit(
            action="resolution",
            offset=t_ch.offset,
            attribute=t_ch.attribute,
            detail=f"needs {'16-bit' if t_ch.fine else '8-bit'}, "
                   f"library has {'16-bit' if c_ch.fine else '8-bit'}",
            severity=2,
        )
    return Edit(
        action="retype",
        offset=t_ch.offset,
        attribute=t_ch.attribute,
        detail=f"library has {c_ch.attribute} ({c_ch.name}), needs {t_ch.attribute}",
        severity=attributes.criticality(t_ch.attribute),
    )


def _collapse_moves(edits: list[Edit]) -> list[Edit]:
    """Rewrite an add/remove pair of the same attribute as a single move.

    Alignment reports a relocated channel as a deletion plus an insertion.
    A tech reads that far more easily as "move it", so pair them back up.
    """
    # Track by position in `edits`, not by value: two identical Edit objects
    # compare equal as dataclasses, so index()-based bookkeeping would pair
    # up the wrong ones.
    removes = [i for i, e in enumerate(edits) if e.action == "remove"]
    adds = [i for i, e in enumerate(edits) if e.action == "add"]
    consumed: set[int] = set()
    moves: list[Edit] = []

    for ri in removes:
        r = edits[ri]
        if r.attribute == "Unknown":
            continue
        for ai in adds:
            if ai in consumed:
                continue
            a = edits[ai]
            if a.attribute != r.attribute:
                continue
            moves.append(Edit(
                action="move",
                offset=r.offset,
                attribute=r.attribute,
                detail=f"move from ch {r.offset} to ch {a.offset}",
                severity=attributes.criticality(r.attribute),
            ))
            consumed.update({ri, ai})
            break

    return [e for i, e in enumerate(edits) if i not in consumed] + moves


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
    if not c_mode.channels:
        reasons.append("channel detail unavailable - compared on footprint only")
    else:
        t_col = attributes.colour_detail(t_mode.attribute_set())
        c_col = attributes.colour_detail(c_mode.attribute_set())
        if t_col == c_col:
            reasons.append(f"same colour system ({t_col})")
        else:
            reasons.append(f"colour system differs ({c_col} vs {t_col})")
    if edits:
        worst = max(e.severity for e in edits)
        reasons.append(f"{len(edits)} change(s) needed, worst severity {worst}/5")

    return Match(fixture=cand, mode=c_mode, score=round(score, 4), reasons=reasons, edits=edits)


def _cheap_score(target: Fixture, t_mode: Mode, cand: Fixture, c_mode: Mode) -> float:
    """A rough score with no sequence alignment.

    Alignment is the expensive part, so triaging a folder against tens of
    thousands of modes needs a first pass that avoids it. This uses only
    footprint distance, colour system and name similarity - all cheap - to
    decide which candidates are worth aligning properly.
    """
    t_size, c_size = _footprint(t_mode), _footprint(c_mode)
    if t_size and c_size:
        size = 1.0 - abs(t_size - c_size) / max(t_size, c_size)
    else:
        size = 0.0

    colour = 1.0 if (attributes.colour_system(t_mode.attribute_set())
                     == attributes.colour_system(c_mode.attribute_set())) else 0.0
    name = max(name_similarity(target.model, cand.model),
               name_similarity(target.manufacturer, cand.manufacturer))
    return 0.5 * size + 0.2 * colour + 0.3 * name


def find_candidates(
    target: Fixture,
    target_mode: Mode,
    library: list[Fixture],
    *,
    limit: int = 10,
    min_score: float = 0.0,
    pool: int = 400,
) -> list[Match]:
    """Rank every (fixture, mode) in ``library`` against the target mode.

    Two-stage: a cheap pass narrows the field to ``pool`` candidates, then the
    full alignment scorer ranks those. On a small library every candidate
    survives the first stage, so results are identical; on a 68,000-mode
    library it is the difference between seconds and minutes.
    """
    pairs = [(cand, c_mode) for cand in library for c_mode in cand.modes]

    if len(pairs) > pool:
        ranked = sorted(
            pairs,
            key=lambda p: -_cheap_score(target, target_mode, p[0], p[1]),
        )
        pairs = ranked[:pool]

    matches = [score_pair(target, target_mode, cand, c_mode) for cand, c_mode in pairs]
    matches = [m for m in matches if m.score >= min_score]
    matches.sort(key=lambda m: (-m.score, len(m.edits), m.label))
    return matches[:limit]


def plan(target: Fixture, target_mode: Mode, match: Match) -> list[Edit]:
    """The ordered edit list to turn ``match`` into ``target_mode``."""
    _, edits = compare_modes(target_mode, match.mode)
    return edits
