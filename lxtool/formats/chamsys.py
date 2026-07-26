"""ChamSys MagicQ library support.

MagicQ personalities live as ``.hed`` files in the MagicQ ``heads`` folder.
The *contents* of a ``.hed`` are obfuscated (see docs/hed-format.md for the
analysis) so this module deliberately does not depend on reading them.  It
gets what it needs from three sources that are plain text:

1. The **filename**, which MagicQ writes as ``Manufacturer_Model_Mode.hed``.
2. ``headmapcapture.csv`` - head key, manufacturer, model, channel count.
3. ``manufacturer_exceptions.csv`` - manufacturer alias table.

That is enough to answer "do I already have this fixture, and in what modes",
which is the question the library scanner exists to answer.  Channel-level
detail comes from the other side of the comparison (GDTF/OFL/MA), and from
``.hed`` bodies once a decoder is available - see ``decode_hed``.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..model import Fixture, Mode

# Modes are very often written with their channel count: "9ch", "16 Ch", "M1-12ch".
_CH_RE = re.compile(r"(\d+)\s*ch\b", re.I)
# Trailing channel counts with no 'ch' suffix, e.g. "Standard_16"
_TRAILING_NUM_RE = re.compile(r"[_\s-](\d{1,3})$")


@dataclass
class HeadFile:
    """One ``.hed`` file, described by everything we can read without decoding."""

    path: Path
    manufacturer: str
    model: str
    mode: str
    channel_count: int | None = None
    size: int = 0

    @property
    def key(self) -> str:
        return f"{self.manufacturer} {self.model}".strip()


def parse_head_filename(path: Path | str) -> HeadFile:
    """Split a MagicQ head filename into manufacturer / model / mode.

    MagicQ's convention is ``Manufacturer_Model_Mode.hed``.  Real libraries are
    messier than that: names can carry extra underscores, omit the manufacturer
    entirely, or be a bare mode name.  Splitting on the *first two* underscores
    keeps any remaining ones inside the mode, which matches what MagicQ does
    when it writes the file back out.
    """
    path = Path(path)
    stem = path.stem

    parts = stem.split("_", 2)
    if len(parts) == 3:
        manufacturer, model, mode = parts
    elif len(parts) == 2:
        manufacturer, model, mode = parts[0], parts[1], ""
    else:
        manufacturer, model, mode = "", stem, ""

    count = _channel_count_from_mode(mode) or _channel_count_from_mode(model)

    return HeadFile(
        path=path,
        manufacturer=manufacturer.strip(),
        model=model.strip(),
        mode=mode.strip(),
        channel_count=count,
        size=path.stat().st_size if path.exists() else 0,
    )


def _channel_count_from_mode(mode: str) -> int | None:
    if not mode:
        return None
    m = _CH_RE.search(mode)
    if m:
        return int(m.group(1))
    m = _TRAILING_NUM_RE.search(mode)
    if m:
        n = int(m.group(1))
        # A trailing number is only plausibly a channel count in this range.
        if 1 <= n <= 512:
            return n
    return None


def load_manufacturer_aliases(path: Path | str) -> dict[str, str]:
    """Read ``manufacturer_exceptions.csv`` into an alias -> canonical map.

    The file is two columns with no header, and it is not consistently
    directional: it contains both ``colour,color`` and ``color,colour``.  We
    keep it as a lookup in the direction given and let the caller resolve
    both ways.
    """
    aliases: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return aliases
    with p.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.reader(fh):
            if len(row) < 2:
                continue
            src, dst = row[0].strip().lower(), row[1].strip().lower()
            if src and dst and src != dst:
                aliases[src] = dst
    return aliases


def load_head_map(path: Path | str) -> list[dict[str, str | int | None]]:
    """Read ``headmapcapture.csv``.

    Columns are: head key, manufacturer, model, channel count, visualiser name.
    Many rows leave manufacturer/model blank and carry only the head key and
    channel count.
    """
    rows: list[dict[str, str | int | None]] = []
    p = Path(path)
    if not p.exists():
        return rows
    with p.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip():
                continue
            row = [c.strip() for c in row] + [""] * (5 - len(row))
            count: int | None = None
            if row[3].isdigit():
                count = int(row[3])
            rows.append({
                "head_key": row[0],
                "manufacturer": row[1],
                "model": row[2],
                "channel_count": count,
                "visualiser": row[4].replace("\\", ""),
            })
    return rows


@dataclass
class ChamSysLibrary:
    """An indexed view of a MagicQ ``heads`` folder."""

    heads: list[HeadFile] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    head_map: list[dict] = field(default_factory=list)

    @classmethod
    def scan(cls, folder: Path | str) -> "ChamSysLibrary":
        folder = Path(folder)
        if not folder.is_dir():
            raise NotADirectoryError(f"not a directory: {folder}")

        heads = [
            parse_head_filename(p)
            for p in sorted(folder.rglob("*.hed"))
            if p.is_file()
        ]
        return cls(
            heads=heads,
            aliases=load_manufacturer_aliases(folder / "manufacturer_exceptions.csv"),
            head_map=load_head_map(folder / "headmapcapture.csv"),
        )

    def canonical_manufacturer(self, name: str) -> str:
        """Resolve a manufacturer through the alias table, following one hop."""
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        return self.aliases.get(key, self.aliases.get(name.strip().lower(), name.strip().lower()))

    def as_fixtures(self) -> list[Fixture]:
        """Collapse head files into fixtures, one mode per head file.

        Two ``.hed`` files that share manufacturer+model are two modes of one
        fixture, which is how a tech thinks about them when patching.
        """
        grouped: dict[tuple[str, str], Fixture] = {}
        for h in self.heads:
            k = (h.manufacturer.lower(), h.model.lower())
            fx = grouped.get(k)
            if fx is None:
                fx = Fixture(
                    manufacturer=h.manufacturer,
                    model=h.model,
                    source="chamsys",
                    source_id=h.path.name,
                )
                grouped[k] = fx
            mode = Mode(name=h.mode or "Default")
            # Without a decoder we know the footprint but not the channel list.
            # Record the footprint as an empty mode of the right size so
            # matching can still compare channel counts honestly.
            if h.channel_count:
                mode.channels = []
                mode.__dict__["_declared_count"] = h.channel_count
            fx.modes.append(mode)
        return list(grouped.values())


# --------------------------------------------------------------------------
# .hed body decoding
# --------------------------------------------------------------------------

class HedDecodeError(NotImplementedError):
    """Raised when a .hed body cannot be decoded.

    The obfuscation is a position-locked, non-repeating keystream over 7-bit
    data (see docs/hed-format.md).  Recovering it needs one known
    plaintext/ciphertext pair; until that exists this raises rather than
    returning a plausible-looking wrong answer.
    """


def looks_obfuscated(data: bytes) -> bool:
    """True when a .hed body is in the encoded form rather than plain text.

    Encoded bodies use only 0x80-0xFF plus literal newlines.
    """
    body = [b for b in data if b != 0x0A]
    if not body:
        return False
    return all(b >= 0x80 for b in body)


def decode_hed(data: bytes) -> str:
    """Decode a ``.hed`` body to text.

    Plain-text heads (older MagicQ versions and hand-written heads) are
    returned as-is.  Obfuscated bodies raise :class:`HedDecodeError`.
    """
    if not looks_obfuscated(data):
        return data.decode("utf-8", errors="replace")
    raise HedDecodeError(
        "This .hed body is obfuscated. See docs/hed-format.md - decoding needs "
        "a known plaintext sample to recover the keystream."
    )
