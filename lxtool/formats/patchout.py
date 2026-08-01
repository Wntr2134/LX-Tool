"""Write a Rig out in the shapes other consoles actually import.

Each writer here exists because a specific desk reads it - none of them
are invented interchange:

``mvr``      MVR is the GDTF-era rig format: **grandMA3** imports it
             natively, as do Capture, Vectorworks, Depence and WYSIWYG.
             (grandMA2 can also read MVR, creating placeholder fixture
             types you then swap for real ones.) Written by
             :mod:`lxtool.formats.mvr`.
``eos``      ETC **Eos family** imports CSV patch, mapping your columns to
             its fields on the way in - so the header names here are the
             ones its mapper expects to see.
``ma2``      grandMA2's community CSV patch plugin takes
             ``fixtureID;universe.address`` with a semicolon delimiter and
             no quoting. Deliberately minimal, because that plugin is
             strict about it.
``magicq``   The MagicQ Fixture Patch layout, for a round trip back to
             where the patch came from.
``csv``      A full human-readable sheet - every column we know, for
             paperwork, Lightwright, or a desk not listed above.

Nothing here invents a footprint: whatever :mod:`lxtool.patchlist`
resolved is what gets written.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from ..model import Rig


def _sheet(rows: list[list], delimiter: str = ",") -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerows(rows)
    return buf.getvalue()


def eos_csv(rig: Rig) -> str:
    """ETC Eos patch import: Channel, Address, Manufacturer, Model, Label.

    Address is written universe/address ("1/71"), which Eos reads
    directly; Channel is the head number so the console's channel numbers
    match the paperwork the crew is holding.
    """
    rows: list[list] = [["Channel", "Address", "Manufacturer", "Fixture Type",
                         "Label", "Position", "Mode", "Footprint"]]
    for i, pf in enumerate(rig.fixtures, start=1):
        channel = pf.fixture_id or str(i)
        rows.append([channel, f"{pf.universe}/{pf.address}",
                     pf.fixture.manufacturer, pf.fixture.model, pf.name,
                     pf.layer, pf.mode, pf.footprint])
    return _sheet(rows)


def ma2_csv(rig: Rig) -> str:
    """grandMA2 CSV patch plugin: ``fixtureID;universe.address``.

    No header, no quoting, semicolon delimited - the plugin wants exactly
    this and nothing else.
    """
    lines = []
    for i, pf in enumerate(rig.fixtures, start=1):
        fid = pf.fixture_id or str(i)
        lines.append(f"{fid};{pf.universe}.{pf.address}")
    return "\n".join(lines) + "\n"


def magicq_csv(rig: Rig) -> str:
    """The MagicQ Fixture Patch layout, for the trip home."""
    rows: list[list] = [["Head No", "DMX", "Position", "Hang",
                         "Manufacturer", "Model", "Mode"]]
    for i, pf in enumerate(rig.fixtures, start=1):
        rows.append([pf.fixture_id or str(i),
                     f"{pf.universe:02d}-{pf.address:03d}",
                     pf.layer, "", pf.fixture.manufacturer,
                     pf.fixture.model, pf.mode])
    return _sheet(rows)


def generic_csv(rig: Rig) -> str:
    """Everything we know, one row per fixture - paperwork and imports."""
    rows: list[list] = [["Head", "Universe", "Address", "Last Address",
                         "Footprint", "Manufacturer", "Model", "Mode",
                         "Name", "Position"]]
    for i, pf in enumerate(rig.fixtures, start=1):
        rows.append([pf.fixture_id or str(i), pf.universe, pf.address,
                     pf.last_address, pf.footprint,
                     pf.fixture.manufacturer, pf.fixture.model, pf.mode,
                     pf.name, pf.layer])
    return _sheet(rows)


TEXT_WRITERS = {
    "eos": (eos_csv, ".csv"),
    "ma2": (ma2_csv, ".csv"),
    "magicq": (magicq_csv, ".csv"),
    "csv": (generic_csv, ".csv"),
}
TARGETS = ("mvr", "eos", "ma2", "magicq", "csv")

TARGET_HELP = {
    "mvr": "MVR rig file - grandMA3, Capture, Vectorworks, Depence (MA2 reads it too)",
    "eos": "CSV for ETC Eos patch import (map the columns on the way in)",
    "ma2": "CSV for the grandMA2 patch plugin (fixtureID;universe.address)",
    "magicq": "MagicQ Fixture Patch CSV - back where it came from",
    "csv": "Full spreadsheet of the patch, for paperwork or anything else",
}


def write(rig: Rig, target: str, path: Path | str) -> Path:
    """Write `rig` for `target`. Returns the path written."""
    path = Path(path)
    if target == "mvr":
        from . import mvr
        return mvr.write(rig, path)
    if target not in TEXT_WRITERS:
        raise ValueError(
            f"unknown target {target!r} (have: {', '.join(TARGETS)})")
    writer, _suffix = TEXT_WRITERS[target]
    path.write_text(writer(rig), encoding="utf-8")
    return path


def default_name(target: str, stem: str = "patch") -> str:
    if target == "mvr":
        return f"{stem}.mvr"
    return f"{stem}-{target}.csv"
