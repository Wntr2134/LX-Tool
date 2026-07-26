"""Command line interface: ``lx``.

    lx fetch                                 cache the Open Fixture Library
    lx search <query>                        search the cached catalogue
    lx scan   <heads-folder>                 index a ChamSys library
    lx match  <fixture-file> <heads-folder>  find the closest existing head
    lx convert <in> <out>                    convert between formats
    lx doctor <file>                         report what we can/can't read
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import catalog, matching
from .formats import chamsys, gdtf, ma2, ofl
from .model import Fixture, Mode

_READERS = {
    ".gdtf": gdtf.read,
    ".json": ofl.read,
    ".xml": None,   # resolved at runtime: GDTF description vs MA2 export
}


def load_fixture(path: Path | str) -> Fixture:
    """Read a fixture from any supported format, chosen by extension/content."""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".gdtf":
        return gdtf.read(path)
    if suffix == ".json":
        return ofl.read(path)
    if suffix == ".xml":
        head = path.read_bytes()[:4096]
        if b"FixtureType" in head and b"DMXMode" in head:
            return gdtf.parse_description(path.read_bytes())
        return ma2.read(path)
    if suffix == ".hed":
        raise SystemExit(
            f"{path.name}: ChamSys .hed bodies are obfuscated and cannot be read yet.\n"
            "See docs/hed-format.md. Use 'lx scan' to index a library by filename."
        )
    raise SystemExit(f"unsupported format: {path.suffix or path.name}")


def cmd_scan(args: argparse.Namespace) -> int:
    lib = chamsys.ChamSysLibrary.scan(args.folder)
    fixtures = lib.as_fixtures()
    modes = sum(len(f.modes) for f in fixtures)
    print(f"{len(lib.heads)} head file(s) -> {len(fixtures)} fixture(s), {modes} mode(s)")
    print(f"{len(lib.aliases)} manufacturer alias(es), {len(lib.head_map)} head-map row(s)")

    if args.verbose:
        for f in sorted(fixtures, key=lambda f: f.key.lower()):
            for m in f.modes:
                size = m.__dict__.get("_declared_count") or "?"
                print(f"  {f.key:<45} {m.name:<24} {size} ch")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    print(f"Downloading Open Fixture Library from {catalog.BULK_URL} ...")
    path = catalog.Catalog.download(args.cache)
    cat = catalog.Catalog.load(args.cache)
    size = path.stat().st_size / 1_000_000
    print(f"Cached {len(cat)} fixtures ({size:.1f} MB) at {path}")
    print(f"{len(cat.manufacturers())} manufacturers")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    try:
        cat = catalog.Catalog.load(args.cache)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    if cat.age_days and cat.age_days > 30:
        print(f"note: catalogue is {cat.age_days:.0f} days old; 'lx fetch' to refresh\n",
              file=sys.stderr)

    hits = cat.search(args.query, limit=args.limit, channels=args.channels)
    if not hits:
        print(f"No match for {args.query!r} in {len(cat)} fixtures.")
        return 0

    for e in hits:
        modes = ", ".join(f"{n} ({c}ch)" for n, c in e.modes)
        print(f"{e.key}")
        print(f"    {e.label}  [{', '.join(e.categories)}]")
        print(f"    {modes}")
    return 0


def _resolve_target(args: argparse.Namespace):
    """Load the fixture to match, from a file or from the OFL catalogue."""
    if getattr(args, "ofl", None):
        cat = catalog.Catalog.load(args.cache)
        entry = cat.get(args.ofl)
        if entry is None:
            hits = cat.search_scored(args.ofl, limit=5)
            if not hits:
                raise SystemExit(f"nothing in the catalogue matches {args.ofl!r}")
            # Only complain about ambiguity when the top hit isn't a clear
            # winner - otherwise "mac 700" would need spelling out in full.
            clear = len(hits) == 1 or hits[0][0] - hits[1][0] >= 0.2
            if not clear:
                lines = "\n".join(f"  {h.key}  ({h.label})" for _, h in hits)
                raise SystemExit(f"{args.ofl!r} is ambiguous. Did you mean:\n{lines}")
            entry = hits[0][1]
        print(f"Using {entry.key} from the OFL catalogue")
        return entry.to_fixture()
    return load_fixture(args.fixture)


def cmd_match(args: argparse.Namespace) -> int:
    if not args.fixture and not args.ofl:
        print("give a fixture file, or --ofl <key-or-search>", file=sys.stderr)
        return 1

    target = _resolve_target(args)
    lib = chamsys.ChamSysLibrary.scan(args.folder)
    library = lib.as_fixtures()

    if not target.modes:
        print(f"{args.fixture}: no DMX modes found", file=sys.stderr)
        return 1

    mode = target.mode(args.mode) if args.mode else target.modes[0]
    if mode is None:
        names = ", ".join(m.name for m in target.modes)
        print(f"no mode named {args.mode!r}. Available: {names}", file=sys.stderr)
        return 1

    print(f"Target: {target.key} [{mode.name}] - {mode.channel_count} ch\n")

    matches = matching.find_candidates(target, mode, library, limit=args.limit)
    if not matches:
        print("No candidates in library.")
        return 0

    for i, m in enumerate(matches, 1):
        flag = "EXACT" if m.exact else f"{m.score:.0%}"
        print(f"{i:>2}. [{flag:>5}] {m.label}")
        for r in m.reasons:
            print(f"        - {r}")

    best = matches[0]
    if best.edits:
        print(f"\nTo use '{best.label}', change (most critical first):\n")
        for e in best.edits:
            print(f"   {e}")
    elif best.exact:
        print("\nExact match - patch it as-is.")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    fixture = load_fixture(args.source)
    out = Path(args.dest)
    suffix = out.suffix.lower()

    if suffix == ".gdtf":
        gdtf.write(fixture, out)
    elif suffix == ".xml":
        ma2.write(fixture, out)
    else:
        raise SystemExit(
            f"cannot write {suffix or out.name}. Supported targets: .gdtf, .xml\n"
            "For ChamSys, write .gdtf - MagicQ imports GDTF natively."
        )

    modes = ", ".join(f"{m.name} ({m.channel_count} ch)" for m in fixture.modes)
    print(f"{fixture.key}: {len(fixture.modes)} mode(s) -> {out}")
    print(f"  {modes}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what we can and cannot read from a file - no guessing."""
    path = Path(args.file)
    data = path.read_bytes()

    if path.suffix.lower() == ".hed":
        obf = chamsys.looks_obfuscated(data)
        head = chamsys.parse_head_filename(path)
        print(f"{path.name}")
        print(f"  body      : {'obfuscated (cannot decode)' if obf else 'plain text'}")
        print(f"  from name : {head.manufacturer!r} / {head.model!r} / {head.mode!r}")
        print(f"  channels  : {head.channel_count if head.channel_count else 'unknown'}")
        return 0

    fixture = load_fixture(path)
    print(f"{path.name}: {fixture.key}  (source={fixture.source})")
    for m in fixture.modes:
        unknown = sum(1 for c in m.channels if c.attribute == "Unknown")
        note = f"  [{unknown} unmapped]" if unknown else ""
        print(f"  {m.name:<28} {m.channel_count:>3} ch{note}")
        if args.verbose:
            for c in sorted(m.channels, key=lambda c: c.offset):
                print(f"     {c.offset:>3}  {c.attribute:<14} {c.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lx", description="Lighting fixture library tools")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="index a ChamSys heads folder")
    s.add_argument("folder")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_scan)

    m = sub.add_parser("match", help="find the closest head in a ChamSys library")
    m.add_argument("fixture", nargs="?", help="GDTF / OFL JSON / MA2 XML file")
    m.add_argument("folder", help="ChamSys heads folder")
    m.add_argument("--ofl", metavar="KEY", help="match a fixture from the OFL catalogue instead of a file")
    m.add_argument("--mode", help="which mode of the source fixture to match")
    m.add_argument("--limit", type=int, default=10)
    m.add_argument("--cache", help="catalogue cache directory")
    m.set_defaults(func=cmd_match)

    fe = sub.add_parser("fetch", help="download the Open Fixture Library for offline use")
    fe.add_argument("--cache", help="catalogue cache directory")
    fe.set_defaults(func=cmd_fetch)

    se = sub.add_parser("search", help="search the cached fixture catalogue")
    se.add_argument("query")
    se.add_argument("--channels", type=int, help="prefer fixtures with a mode of this size")
    se.add_argument("--limit", type=int, default=20)
    se.add_argument("--cache", help="catalogue cache directory")
    se.set_defaults(func=cmd_search)

    c = sub.add_parser("convert", help="convert a fixture between formats")
    c.add_argument("source")
    c.add_argument("dest")
    c.set_defaults(func=cmd_convert)

    d = sub.add_parser("doctor", help="report what can be read from a file")
    d.add_argument("file")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
