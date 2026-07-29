"""Your own saved heads - the clones and manual-built fixtures you've cracked.

A head you figure out at one venue should be there at the next. This is a
small personal library: `.hed` files under a user-writable directory,
alongside a plain-text plan for each so you can reopen and tweak it later.

It plugs into the rest of the tool as just another library source, so a
saved clone turns up in ``lx match`` and "What is this?" beside the stock
library and OFL.

    ~/.local/share/lxtool/heads/China_AuraClone_14ch.hed
    ~/.local/share/lxtool/heads/China_AuraClone_14ch.plan

Override the location with ``LXTOOL_MYHEADS``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import plan as plan_mod
from .formats import chamsys
from .model import Fixture, Mode


def store_dir() -> Path:
    """Where saved heads live. User-writable, created on demand."""
    env = os.environ.get("LXTOOL_MYHEADS")
    if env:
        return Path(env)
    base = (os.environ.get("XDG_DATA_HOME")
            or (Path.home() / ".local" / "share"))
    return Path(base) / "lxtool" / "heads"


def _safe_stem(fixture: Fixture, mode: Mode) -> str:
    stem = f"{fixture.manufacturer}_{fixture.model}_{mode.name}".strip("_")
    stem = re.sub(r"[^A-Za-z0-9 ._+-]", "", stem).strip()
    return stem or "custom_head"


@dataclass
class SavedHead:
    stem: str
    hed: Path
    plan: Path | None
    manufacturer: str
    model: str
    mode: str
    channels: int


def save(fixture: Fixture, mode: Mode | None = None, *,
         plan_text: str = "") -> SavedHead:
    """Save a head (and its editable plan) into the personal library."""
    mode = mode or (fixture.modes[0] if fixture.modes else Mode(name="Default"))
    directory = store_dir()
    directory.mkdir(parents=True, exist_ok=True)

    stem = _safe_stem(fixture, mode)
    hed = chamsys.write(fixture, directory / f"{stem}.hed", mode)
    plan_path = directory / f"{stem}.plan"
    plan_path.write_text(plan_text or plan_mod.dump(fixture, mode),
                         encoding="utf-8")

    return SavedHead(stem, hed, plan_path, fixture.manufacturer,
                     fixture.model, mode.name, mode.channel_count)


def entries() -> list[SavedHead]:
    """Every saved head, newest first."""
    directory = store_dir()
    if not directory.is_dir():
        return []
    out: list[SavedHead] = []
    for hed in sorted(directory.glob("*.hed"), key=lambda p: -p.stat().st_mtime):
        try:
            fx = chamsys.read(hed)
        except Exception:      # noqa: BLE001 - a broken file must not hide the rest
            continue
        mode = fx.modes[0] if fx.modes else Mode(name="")
        plan = hed.with_suffix(".plan")
        out.append(SavedHead(
            hed.stem, hed, plan if plan.is_file() else None,
            fx.manufacturer, fx.model, mode.name, mode.channel_count))
    return out


def load_fixtures() -> list[Fixture]:
    """Saved heads as fixtures, tagged so matching shows where they came from."""
    out: list[Fixture] = []
    for saved in entries():
        try:
            fx = chamsys.read(saved.hed)
        except Exception:      # noqa: BLE001
            continue
        fx.source = "myheads"
        fx.source_id = saved.stem
        out.append(fx)
    return out


def get_plan(stem: str) -> str | None:
    """The saved plan text for a head, for reopening in the editor."""
    path = store_dir() / f"{stem}.plan"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    hed = store_dir() / f"{stem}.hed"
    if hed.is_file():
        try:
            return plan_mod.dump(chamsys.read(hed))
        except Exception:      # noqa: BLE001
            return None
    return None


def remove(stem: str) -> bool:
    """Delete a saved head and its plan. Returns whether anything was removed."""
    directory = store_dir()
    hit = False
    for suffix in (".hed", ".plan"):
        path = directory / f"{stem}{suffix}"
        if path.is_file():
            path.unlink()
            hit = True
    return hit
