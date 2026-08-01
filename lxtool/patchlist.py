"""Read a console's patch export into a Rig.

The source is a MagicQ **Fixture Patch** export - the CSV, or the text
lifted out of the PDF version of the same table:

    Head No, DMX,    Position, Hang, Manufacturer, Model,        Mode
    1,       01-001,         ,     ,             ,             , HILED
    11,      01-071,         ,     , Beamz,        Panther40MK2, 9ch
    24,      01-421,         ,     , BeamZ,        H2000Fazer,   2ch

Everything about that table is optional except the address: real exports
leave manufacturer, model, position and hang blank for heads that were
patched generically, and the PDF version arrives as ragged text. So the
parser keys on the DMX cell (``01-071`` = universe 1, address 71) and
treats the rest as best-effort.

The number that matters for a re-patch is the **footprint**, which the
export does not state outright. Three sources, in descending trust:

1. the Mode cell, when it says how many channels ("9ch", "2 Ch");
2. the fixture catalogue - manufacturer + model resolved to a real
   profile, then the mode matched by name or channel count;
3. the gap to the next head's address, which is what the person who
   patched it actually left room for.

Whichever answered is recorded per fixture, so an export can be reviewed
before it is trusted.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .model import Channel, Fixture, Mode, PatchedFixture, Rig

# "01-001", "1-1", "1.001", "1/1", "01:001" - universe and address.
_DMX = re.compile(r"^\s*(\d{1,3})\s*[-./:]\s*(\d{1,3})\s*$")
# A bare address with no universe.
_BARE = re.compile(r"^\s*(\d{1,4})\s*$")
# "9ch", "9 Ch", "24-channel"
_CHANS = re.compile(r"\b(\d{1,3})\s*(?:ch|channel)\b", re.IGNORECASE)

_HEADERS = ("head", "dmx", "position", "hang", "manufacturer", "model",
            "mode", "channel", "address", "universe", "fixture", "type",
            "name", "id")


@dataclass
class PatchRow:
    """One line of the export, before it becomes a patched fixture."""

    head_no: int = 0
    universe: int = 1
    address: int = 1
    manufacturer: str = ""
    model: str = ""
    mode: str = ""
    position: str = ""
    hang: str = ""
    channels: int = 0
    channels_from: str = ""        # "mode" | "catalogue" | "gap" | "default"
    lineno: int = 0


@dataclass
class PatchReport:
    """What came back, and how much of it we had to infer."""

    rig: Rig
    rows: list = field(default_factory=list)
    skipped: int = 0
    warnings: list = field(default_factory=list)

    @property
    def resolved(self) -> int:
        return sum(1 for r in self.rows if r.channels_from == "catalogue")

    @property
    def guessed(self) -> int:
        return sum(1 for r in self.rows if r.channels_from in ("gap", "default"))


def parse(text: str, *, cache=None, name: str = "") -> PatchReport:
    """A patch export (CSV text or PDF text) -> a Rig, with provenance."""
    rows = _rows(text)
    if not rows:
        return PatchReport(rig=Rig(name=name, source="patchlist"),
                           warnings=["no patch rows recognised - is this the "
                                     "fixture patch export?"])

    _infer_footprints(rows, cache)
    fixtures = [_patched(r) for r in rows]
    rig = Rig(name=name, fixtures=fixtures, source="patchlist")
    report = PatchReport(rig=rig, rows=rows)
    report.warnings = _collisions(rows)
    return report


# ---- reading ----------------------------------------------------------


def _rows(text: str) -> list[PatchRow]:
    """Every recognisable patch line, from CSV or loose PDF text."""
    out: list[PatchRow] = []
    header: list[str] | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        cells = _cells(line)
        if not cells:
            continue
        if _is_header(cells):
            header = [c.strip().lower() for c in cells]
            continue
        row = _row(cells, header, lineno)
        if row is not None:
            out.append(row)
    return out


def _cells(line: str) -> list[str]:
    """Split a line into cells: comma/semicolon/tab CSV, or PDF spacing."""
    if "," in line or ";" in line or "\t" in line:
        delim = ";" if line.count(";") > line.count(",") else \
            ("\t" if "\t" in line and "," not in line else ",")
        try:
            return next(csv.reader(io.StringIO(line), delimiter=delim))
        except (csv.Error, StopIteration):
            return []
    return [c for c in re.split(r"\s{2,}", line)]


def _is_header(cells: list[str]) -> bool:
    hits = sum(1 for c in cells
               if any(h in c.strip().lower() for h in _HEADERS))
    return hits >= 2 and not any(_DMX.match(c) for c in cells)


def _row(cells: list[str], header: list[str] | None,
         lineno: int) -> PatchRow | None:
    """One data line -> a PatchRow, positionally or by header."""
    if header and len(header) >= 2:
        got = _by_header(cells, header)
        if got is not None:
            got.lineno = lineno
            return got
    return _positional(cells, lineno)


def _by_header(cells: list[str], header: list[str]) -> PatchRow | None:
    def cell(*names: str) -> str:
        """Exact header match first: "mode" is a substring of "model", so
        substring matching alone reads the wrong column."""
        for want in names:
            for i, h in enumerate(header):
                if i < len(cells) and h.strip() == want:
                    return cells[i].strip()
        for want in names:
            for i, h in enumerate(header):
                if i < len(cells) and want in h:
                    return cells[i].strip()
        return ""

    uni, addr = _address(cell("dmx", "address", "patch"))
    if addr is None:
        # Separate universe and address columns.
        u, a = cell("universe"), cell("address", "dmx")
        if a.strip().isdigit():
            uni = int(u) if u.strip().isdigit() else 1
            addr = int(a)
    if addr is None:
        return None
    head = cell("head", "channel", "id", "fixture no")
    return PatchRow(
        head_no=int(head) if head.strip().isdigit() else 0,
        universe=uni or 1, address=addr,
        manufacturer=cell("manufacturer", "maker"),
        model=cell("model", "type", "fixture"),
        mode=cell("mode"),
        position=cell("position"), hang=cell("hang"),
    )


def _positional(cells: list[str], lineno: int) -> PatchRow | None:
    """No usable header: find the address cell and read around it.

    The MagicQ layout is Head No, DMX, Position, Hang, Manufacturer,
    Model, Mode - so text after the address is the descriptive tail, and
    the number before it is the head number.
    """
    dmx_at = next((i for i, c in enumerate(cells) if _DMX.match(c)), None)
    if dmx_at is None:
        return None
    uni, addr = _address(cells[dmx_at])
    if addr is None:
        return None
    head = 0
    if dmx_at and cells[dmx_at - 1].strip().isdigit():
        head = int(cells[dmx_at - 1].strip())
    tail = [c.strip() for c in cells[dmx_at + 1:] if c.strip()]
    mode = tail[-1] if tail else ""
    model = tail[-2] if len(tail) >= 2 else ""
    maker = tail[-3] if len(tail) >= 3 else ""
    return PatchRow(head_no=head, universe=uni or 1, address=addr,
                    manufacturer=maker, model=model, mode=mode,
                    lineno=lineno)


def _address(cell: str) -> tuple[int, int | None]:
    m = _DMX.match(cell or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _BARE.match(cell or "")
    if m:
        n = int(m.group(1))
        if n > 512:                      # a continuous address: split it
            return (n - 1) // 512 + 1, (n - 1) % 512 + 1
        return 1, n
    return 1, None


# ---- footprints -------------------------------------------------------


def _infer_footprints(rows: list[PatchRow], cache) -> None:
    catalogue = _catalogue(cache)
    for row in rows:
        m = _CHANS.search(row.mode or "")
        if m:
            row.channels, row.channels_from = int(m.group(1)), "mode"
            continue
        found = _from_catalogue(catalogue, row)
        if found:
            row.channels, row.channels_from = found, "catalogue"

    # Whatever is still unknown: the space the patcher left for it.
    order = sorted(rows, key=lambda r: (r.universe, r.address))
    for cur, nxt in zip(order, order[1:]):
        if cur.channels:
            continue
        if nxt.universe == cur.universe and nxt.address > cur.address:
            cur.channels = nxt.address - cur.address
            cur.channels_from = "gap"
    for row in rows:
        if not row.channels:
            row.channels, row.channels_from = 1, "default"


def _catalogue(cache):
    try:
        from . import catalog
        return catalog.Catalog.load(cache)
    except Exception:      # noqa: BLE001 - no catalogue is not fatal
        return None


def _from_catalogue(catalogue, row: PatchRow) -> int:
    """Channel count for this row's real fixture profile, if we know it.

    A model name is required: a manufacturer on its own ("Martin") will
    happily match *something* in 17,000 fixtures, and a confident wrong
    footprint is worse than an honest inferred one.
    """
    if catalogue is None or len(row.model.strip()) < 3:
        return 0
    query = f"{row.manufacturer} {row.model}".strip()
    hits = catalogue.search_scored(query, limit=1)
    if not hits:
        return 0
    score, entry = hits[0]
    if score < 0.7:                       # a weak name match is a bad guess
        return 0
    modes = list(entry.modes)
    if not modes:
        return 0
    want = (row.mode or "").strip().lower()
    for name, count in modes:
        if want and want == name.strip().lower():
            return count
    return modes[0][1]


def _patched(row: PatchRow) -> PatchedFixture:
    """A PatchRow as a PatchedFixture with a placeholder profile.

    The profile carries the right channel count and the names we know, so
    every exporter downstream can write a correct footprint even when the
    fixture was never in the catalogue.
    """
    mode_name = row.mode or f"{row.channels}ch"
    channels = [Channel(offset=i, name=f"Channel {i}", attribute="Unknown")
                for i in range(1, row.channels + 1)]
    if channels:
        channels[0] = Channel(offset=1, name="Dimmer", attribute="Dimmer",
                              htp=True)
    fixture = Fixture(
        manufacturer=row.manufacturer or "Generic",
        model=row.model or (row.mode or "Head"),
        modes=[Mode(name=mode_name, channels=channels)],
        source="patchlist",
    )
    return PatchedFixture(
        name=f"{row.model or 'Head'} {row.head_no}".strip(),
        fixture=fixture, mode=mode_name,
        fixture_id=str(row.head_no or ""),
        universe=row.universe, address=row.address,
        layer=row.position or "",
    )


def _collisions(rows: list[PatchRow]) -> list[str]:
    """Overlapping footprints, on the numbers we ended up with."""
    out: list[str] = []
    by_uni: dict[int, list[PatchRow]] = {}
    for r in rows:
        by_uni.setdefault(r.universe, []).append(r)
    for uni, urows in sorted(by_uni.items()):
        urows = sorted(urows, key=lambda r: r.address)
        for a, b in zip(urows, urows[1:]):
            end = a.address + a.channels - 1
            if b.address <= end:
                out.append(
                    f"universe {uni}: head {a.head_no} at {a.address} runs to "
                    f"{end} ({a.channels} ch, from {a.channels_from}) and "
                    f"overlaps head {b.head_no} at {b.address}")
    return out
