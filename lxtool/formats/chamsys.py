"""ChamSys MagicQ library support.

MagicQ personalities live as ``.hed`` files in the MagicQ ``heads`` folder.
Their contents are obfuscated, but the scheme is now understood and
implemented in :func:`decode_hed` / :func:`encode_hed` - see
docs/hed-format.md for how it was recovered. A personality decodes to plain
text, so channel-level detail is available directly.

Two further plain-text files in the same folder are also used:

* ``headmapcapture.csv`` - head key, manufacturer, model, channel count.
* ``manufacturer_exceptions.csv`` - manufacturer alias table.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..model import Channel, Fixture, Mode

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


# --------------------------------------------------------------------------
# Personality parsing
#
# A decoded personality looks like:
#
#     # MagicQ personality file.  Copyright Chamsys Ltd 2021 www.chamsys.co.uk
#     \ Personality file for China 7x9 Watt Mini Led par RGB
#     V,008c,"MagicQ 1";
#     P,0008,"China_7x9WMiniParRGB_7ch","China","7ch","7x9WMiniParRGB",
#     0007,0007,000a,0000,...          <- first field is the channel count
#     "Pan",00000032,00000004,         <- name, flags, attribute number
#     "Pan",00000032,00000004,         <- repeat = the 16-bit fine half
#
# The flags word is (encoder_bank << 4) | type, where type 1 is HTP and 2 is
# LTP.  The attribute number is MagicQ's own parameter id.
# --------------------------------------------------------------------------

_P_LINE = re.compile(r'^P,([0-9a-f]+),(.*)$', re.I)
_QUOTED = re.compile(r'"((?:[^"]|"")*)"')
HTP = 1
LTP = 2

# MagicQ attribute numbers seen in real personalities.  The channel *name* is
# human-readable and normalises well on its own, so this table is used to
# corroborate and to catch cases where the name is idiosyncratic.
MAGICQ_ATTRIBUTES: dict[int, str] = {
    0x00: "Dimmer",
    0x02: "Shutter",
    0x04: "Pan",
    0x05: "Tilt",
    0x06: "ColorWheel",
    0x08: "Gobo1",
    0x0E: "Prism",
    0x10: "Red",
    0x11: "Green",
    0x12: "Blue",
    0x13: "White",
    0x1A: "Strobe",
    0x26: "Macro",
    0x27: "Reset",
    0x3F: "Unknown",      # MagicQ's own "Reserved (63)"
}

# Encoder banks seen in the flags word: (flags >> 4) & 0xF.
ENCODER_BANKS = {0: "intensity", 1: "beam", 2: "colour", 3: "position"}


def parse_personality(text: str) -> Fixture:
    """Parse a decoded MagicQ personality into a :class:`Fixture`."""
    from .. import attributes as _attrs

    manufacturer = model = mode_name = ""
    channels: list[tuple[str, int, int]] = []
    declared = 0

    lines = [ln.rstrip("\r") for ln in text.split("\n")]

    for idx, line in enumerate(lines):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("\\"):
            continue

        p = _P_LINE.match(line)
        if p:
            fields = _QUOTED.findall(p.group(2))
            # name, manufacturer, mode, model
            if len(fields) >= 4:
                manufacturer, mode_name, model = fields[1], fields[2], fields[3]
            elif fields:
                model = fields[0]
            # The line after P carries the channel count as its first field.
            for follow in lines[idx + 1:]:
                follow = follow.strip()
                if follow:
                    head = follow.split(",")[0]
                    if re.fullmatch(r"[0-9a-f]{4}", head, re.I):
                        declared = int(head, 16)
                    break
            continue

        chan = _parse_channel_line(line)
        if chan:
            channels.append(chan)

    mode = Mode(name=mode_name or "Default")
    prev_attr: str | None = None
    prev_num: int | None = None
    offset = 0

    for name, flags, attr_num in channels:
        offset += 1
        canonical = _attrs.normalise(name, default="")
        if not canonical:
            canonical = MAGICQ_ATTRIBUTES.get(attr_num, "Unknown")

        # MagicQ writes a 16-bit parameter as two consecutive channels sharing
        # a name and attribute number; the second is the fine half.
        fine = (
            prev_num is not None
            and attr_num == prev_num
            and canonical == prev_attr
            and canonical != "Unknown"
        )

        mode.channels.append(Channel(
            offset=offset,
            name=name,
            attribute=canonical,
            fine=fine,
            htp=(flags & 0x0F) == HTP,
        ))
        prev_attr, prev_num = canonical, attr_num

    if declared and declared > len(mode.channels):
        # Trust the declared footprint: a personality can leave trailing slots
        # unnamed, and losing them would shift a downstream patch.
        mode.declared_count = declared

    fixture = Fixture(
        manufacturer=manufacturer,
        model=model,
        modes=[mode],
        source="chamsys",
    )
    return fixture


def _parse_channel_line(line: str) -> tuple[str, int, int] | None:
    """Parse ``"Pan",00000032,00000004,`` into (name, flags, attribute)."""
    if not line.startswith('"'):
        return None
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None

    m = _QUOTED.match(parts[0])
    if not m:
        return None
    name = m.group(1).replace('""', '"')

    try:
        flags = int(parts[1], 16)
        attr_num = int(parts[2], 16)
    except ValueError:
        return None

    # Channel rows use 8-hex-digit fields; other sections of the file also
    # start with a quoted string but carry differently sized fields.
    if len(parts[1]) != 8 or len(parts[2]) != 8:
        return None

    return name, flags, attr_num


_ATTR_NUMBERS = {v: k for k, v in MAGICQ_ATTRIBUTES.items() if v != "Unknown"}
_RESERVED = 0x3F

_BANK_OF = {
    "intensity": 0,
    "beam": 1,
    "colour": 2,
    "position": 3,
    "control": 1,
}


def _flags_for(attribute: str, htp: bool) -> int:
    """MagicQ's flags word: (encoder bank << 4) | HTP/LTP."""
    from .. import attributes as _attrs

    bank = _BANK_OF.get(_attrs.group_of(attribute), 1)
    return (bank << 4) | (HTP if htp else LTP)


def build_personality(fixture: Fixture, mode: Mode | None = None, *, year: int = 2026) -> str:
    """Render a fixture as a MagicQ personality.

    EXPERIMENTAL.  The header and channel block are modelled directly on real
    personalities and are believed correct, but a ``.hed`` also carries
    trailing sections (palettes, ranges, per-channel defaults) whose meaning
    has not been fully decoded.  What is emitted here is the minimum MagicQ
    needs to describe the DMX layout; test the result in the Head Editor
    before relying on it, and prefer GDTF import if it misbehaves.
    """
    mode = mode or (fixture.modes[0] if fixture.modes else Mode(name="Default"))
    channels = sorted(mode.channels, key=lambda c: c.offset)
    count = mode.channel_count or len(channels)

    name = f"{fixture.manufacturer}_{fixture.model}_{mode.name}".strip("_")
    out: list[str] = [
        f"# MagicQ personality file.  Copyright Chamsys Ltd {year} www.chamsys.co.uk",
        f"\\ Personality file for {fixture.model or 'fixture'} ",
        'V,008c,"MagicQ 1";',
        f'P,{count:04x},"{name}","{fixture.manufacturer}","{mode.name}","{fixture.model}",',
        f"{count:04x},0000,0000,0000,0000,0000,0001,0001,01f5,00000000,",
    ]

    for ch in channels:
        attr_num = _ATTR_NUMBERS.get(ch.attribute, _RESERVED)
        flags = _flags_for(ch.attribute, ch.htp)
        out.append(f'"{ch.name}",{flags:08x},{attr_num:08x},')

    # Trailing sections, sized to the channel count.
    out.append("0000,")
    out.append("00000000,")
    out.append(",".join(["00000000"] * max(count, 1)) + ",")
    out.append('"",00000000,0000,0000,0000,0000,0000,')
    for ch in channels:
        out.append(f"{ch.default:08x},0000,0100,01ff,")
    out.append('"",')
    out.append("")
    return "\n".join(out)


def write(fixture: Fixture, path: Path | str, mode: Mode | None = None) -> Path:
    """Write a fixture as an obfuscated ``.hed`` MagicQ reads directly."""
    path = Path(path)
    path.write_bytes(encode_hed(build_personality(fixture, mode)))
    return path


def read(path: Path | str) -> Fixture:
    """Read a ``.hed`` file, decoding it if necessary."""
    path = Path(path)
    fx = parse_personality(decode_hed(path.read_bytes()))
    fx.source_id = path.name

    # The filename is authoritative for manufacturer/model when the personality
    # header is blank, which happens in hand-edited heads.
    if not fx.manufacturer or not fx.model:
        head = parse_head_filename(path)
        fx.manufacturer = fx.manufacturer or head.manufacturer
        fx.model = fx.model or head.model
    return fx


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

    def as_fixtures(self, *, read_bodies: bool = True) -> list[Fixture]:
        """Collapse head files into fixtures, one mode per head file.

        Two ``.hed`` files that share manufacturer+model are two modes of one
        fixture, which is how a tech thinks about them when patching.

        With ``read_bodies`` the personality is decoded so each mode carries
        its real channel list.  A file that fails to decode degrades to a
        footprint-only mode rather than dropping out of the library.
        """
        grouped: dict[tuple[str, str], Fixture] = {}
        for h in self.heads:
            parsed: Fixture | None = None
            if read_bodies and h.path.exists():
                try:
                    parsed = read(h.path)
                except (OSError, ValueError, UnicodeDecodeError):
                    parsed = None

            manufacturer = (parsed.manufacturer if parsed else "") or h.manufacturer
            model = (parsed.model if parsed else "") or h.model

            k = (manufacturer.lower(), model.lower())
            fx = grouped.get(k)
            if fx is None:
                fx = Fixture(
                    manufacturer=manufacturer,
                    model=model,
                    source="chamsys",
                    source_id=h.path.name,
                )
                grouped[k] = fx

            if parsed and parsed.modes and parsed.modes[0].channels:
                mode = parsed.modes[0]
                mode.name = mode.name or h.mode or "Default"
            else:
                # Undecodable: keep the footprint from the filename so
                # matching can still compare channel counts honestly.
                mode = Mode(name=h.mode or "Default")
                if h.channel_count:
                    mode.declared_count = h.channel_count
            fx.modes.append(mode)
        return list(grouped.values())


# --------------------------------------------------------------------------
# .hed body decoding
# --------------------------------------------------------------------------

class HedDecodeError(ValueError):
    """Raised when a ``.hed`` body cannot be decoded."""


# The obfuscation: each character is XORed with a keystream that counts *down*
# by one per character, modulo 127, and the result has its high bit set.
# Newlines are written literally and do not advance the counter.
#
#     key(i) = (-i) mod 127,  except that a key of 0 is written as 127
#              for every position after the first
#     cipher = (plain XOR key) | 0x80
#
# It is symmetric, so the same routine encodes and decodes.  The modulus is
# 127 rather than 128, which is what defeats every linear search over a
# 128-value keyspace - see docs/hed-format.md.
_MODULUS = 127


def _key(i: int) -> int:
    k = (-i) % _MODULUS
    if k == 0 and i != 0:
        return 127
    return k


def looks_obfuscated(data: bytes) -> bool:
    """True when a .hed body is in the encoded form rather than plain text.

    Encoded bodies use only 0x80-0xFF plus literal newlines.
    """
    body = [b for b in data if b != 0x0A]
    if not body:
        return False
    return all(b >= 0x80 for b in body)


def decode_hed(data: bytes) -> str:
    """Decode a ``.hed`` body to its plain-text personality.

    Plain-text heads (older MagicQ versions and hand-written heads) are
    returned unchanged.
    """
    if not looks_obfuscated(data):
        return data.decode("utf-8", errors="replace")

    out = bytearray()
    i = 0
    for b in data:
        if b == 0x0A:
            out.append(0x0A)
            continue
        out.append((b & 0x7F) ^ _key(i))
        i += 1
    return out.decode("utf-8", errors="replace")


def encode_hed(text: str) -> bytes:
    """Encode a plain-text personality back into ``.hed`` form.

    The inverse of :func:`decode_hed`; MagicQ reads the result directly.
    """
    out = bytearray()
    i = 0
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            out.append(0x0A)
            continue
        code = ord(ch)
        if code > 0x7F:
            raise HedDecodeError(
                f"{ch!r} is not 7-bit ASCII; MagicQ personalities cannot carry it"
            )
        out.append((code ^ _key(i)) | 0x80)
        i += 1
    return bytes(out)
