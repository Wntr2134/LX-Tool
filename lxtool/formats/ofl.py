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
        name = cap.get("comment") or cap.get("type") or ""
        out.append(Range(max(0, min(255, lo)), max(0, min(255, hi)), name))
    return out


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

    for mode_data in data.get("modes", []) or []:
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
                    default=int(chan_data.get("defaultValue", 0) or 0) if not is_fine else 0,
                    highlight=chan_data.get("highlightValue"),
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
