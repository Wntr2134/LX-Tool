"""Open Fixture Library reader.

OFL publishes every fixture as JSON under a permissive licence, with a stable
scheme documented at https://open-fixture-library.org/about/fixture-format.

The shape that matters here::

    {
      "name": "MH-X25",
      "manufacturerKey": "eurolite",
      "availableChannels": { "Pan": {...}, "Dimmer": {...} },
      "modes": [ { "name": "9-channel", "channels": ["Pan", "Pan fine", ...] } ]
    }

Entries in a mode's ``channels`` list are either a key into
``availableChannels``, ``null`` for an unused slot, or an object describing a
matrix/template channel.  Fine channels appear in ``fineChannelAliases`` on
the parent channel rather than as channels in their own right.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import attributes
from ..model import Channel, Fixture, Mode, Range

API_ROOT = "https://open-fixture-library.org"


def _capabilities(chan: dict[str, Any]) -> list[Range]:
    """Flatten OFL capabilities into DMX ranges, normalised to 8-bit."""
    caps = chan.get("capabilities")
    if caps is None:
        single = chan.get("capability")
        caps = [single] if single else []

    res = int(chan.get("dmxValueResolution", "8bit").replace("bit", "") or 8)
    scale = 1 << (res - 8) if res > 8 else 1

    out: list[Range] = []
    for cap in caps:
        if not isinstance(cap, dict):
            continue
        rng = cap.get("dmxRange")
        if isinstance(rng, list) and len(rng) == 2:
            lo, hi = int(rng[0]) // scale, int(rng[1]) // scale
        else:
            lo, hi = 0, 255
        name = cap.get("comment") or _cap_name(cap)
        out.append(Range(max(0, min(255, lo)), max(0, min(255, hi)), name))
    return out


# OFL shutterEffect -> the name ChamSys's own library uses for the same
# thing. Verified against the MAC Aura, where OFL's RampUp 70-84 is
# byte-identical to ChamSys's "Pulse Open" 0x46-0x54. These names matter
# beyond cosmetics: MagicQ types shutter ranges by vocabulary, and a
# shutter with no Open/Closed range is a Head Editor error.
_SHUTTER_EFFECT = {
    "Open": "Open", "Closed": "Closed", "Strobe": "Strobe",
    "RampUp": "Pulse Open", "RampDown": "Pulse Closed",
    "RampUpDown": "Pulse", "Pulse": "Pulse",
    "Lightning": "Lightning", "Spikes": "Burst", "Burst": "Burst",
}


def _cap_name(cap: dict[str, Any]) -> str:
    """A usable slot name for a capability that has no comment.

    The capability type alone ("ShutterStrobe" twenty-two times over) tells
    an operator nothing; the typed fields are where the meaning lives.
    """
    ctype = cap.get("type") or ""
    if ctype == "ShutterStrobe":
        name = _SHUTTER_EFFECT.get(cap.get("shutterEffect", ""), "Strobe")
        if cap.get("randomTiming"):
            name = f"Rnd {name}"
        start, end = cap.get("speedStart"), cap.get("speedEnd")
        if start and end:
            name += f" {str(start)[:1].upper()}>{str(end)[:1].upper()}"
        return name
    if ctype == "NoFunction":
        return "No Function"
    if ctype == "ColorPreset" and cap.get("colors"):
        return ctype
    return ctype


def dmx_default(raw: Any, default: int = 0) -> int:
    """Parse an OFL default/highlight value.

    These are usually a plain DMX number but may be a percentage string -
    "50%" is legal and common - so a bare int() raises on real library data.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        return max(0, min(255, int(raw)))

    text = str(raw).strip()
    if not text:
        return default
    if text.endswith("%"):
        try:
            pct = float(text[:-1])
        except ValueError:
            return default
        return max(0, min(255, round(pct * 255 / 100)))
    try:
        return max(0, min(255, int(float(text))))
    except ValueError:
        return default


def _fine_alias_map(available: dict[str, Any]) -> dict[str, str]:
    """Map each fine-channel alias back to its coarse channel key."""
    aliases: dict[str, str] = {}
    for key, chan in available.items():
        if not isinstance(chan, dict):
            continue
        for alias in chan.get("fineChannelAliases", []) or []:
            aliases[alias] = key
    return aliases


def parse(data: dict[str, Any] | str | bytes, *, manufacturer: str = "") -> Fixture:
    """Parse one OFL fixture document into a :class:`Fixture`."""
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise ValueError("OFL fixture must be a JSON object")

    available: dict[str, Any] = data.get("availableChannels", {}) or {}
    fine_of = _fine_alias_map(available)

    fixture = Fixture(
        # The document's own key is authoritative; the caller's hint (usually
        # the containing folder name, which is the manufacturer in an OFL
        # checkout) is only a fallback.
        manufacturer=data.get("manufacturerKey", "") or manufacturer or "",
        model=data.get("name", "") or "",
        source="ofl",
        fixture_type=", ".join(data.get("categories", []) or []),
    )

    modes = data.get("modes") or []
    if not isinstance(modes, list):
        raise ValueError("'modes' must be a list")

    for mode_data in modes:
        if not isinstance(mode_data, dict):
            # Real library data is occasionally malformed; skip the bad mode
            # rather than losing the whole fixture.
            continue
        mode = Mode(name=mode_data.get("name", "Default") or "Default")
        for index, entry in enumerate(mode_data.get("channels", []) or [], start=1):
            if entry is None:
                # An explicitly unused DMX slot. It still consumes footprint,
                # so record it rather than silently shifting later channels.
                mode.channels.append(
                    Channel(offset=index, name="(unused)", attribute="Unknown")
                )
                continue

            if isinstance(entry, dict):
                # Matrix / template channel - use its resolved name if present.
                key = entry.get("insert") or entry.get("key") or json.dumps(entry)[:40]
                key = str(key)
            else:
                key = str(entry)

            is_fine = key in fine_of
            coarse_key = fine_of.get(key, key)
            chan_data = available.get(coarse_key, {}) or {}

            attr = attributes.normalise(chan_data.get("name") or coarse_key)

            mode.channels.append(
                Channel(
                    offset=index,
                    name=key,
                    attribute=attr,
                    fine=is_fine,
                    default=dmx_default(chan_data.get("defaultValue")) if not is_fine else 0,
                    highlight=(dmx_default(chan_data["highlightValue"])
                               if chan_data.get("highlightValue") is not None else None),
                    htp=attr == "Dimmer",
                    invert=bool(chan_data.get("invert", False)),
                    ranges=_capabilities(chan_data) if not is_fine else [],
                )
            )
        fixture.modes.append(mode)

    return fixture


def read(path: Path | str, *, manufacturer: str = "") -> Fixture:
    path = Path(path)
    fx = parse(path.read_bytes(), manufacturer=manufacturer or path.parent.name)
    fx.source_id = path.name
    return fx


def fixture_url(manufacturer_key: str, fixture_key: str) -> str:
    """Direct JSON URL for one OFL fixture."""
    return f"{API_ROOT}/{manufacturer_key}/{fixture_key}.json"
