"""Command line interface: ``lx``.

    lx fetch                                 cache the Open Fixture Library
    lx search <query>                        search the cached catalogue
    lx scan   <heads-folder>                 index a ChamSys library
    lx match  <fixture-file> <heads-folder>  find the closest existing head
    lx rig    <file.mvr>                     summarise a whole patch
    lx convert <in> <out>                    convert between formats
    lx doctor <file>                         report what we can/can't read
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import catalog, gdtfshare, library as libmod, matching
from .formats import chamsys, gdtf, ma2, ma3, mvr, ofl
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
        data = path.read_bytes()
        if ma3.looks_like_ma3(data):
            return ma3.parse(data)
        if b"FixtureType" in data[:4096] and b"DMXMode" in data[:4096]:
            return gdtf.parse_description(data)
        return ma2.read(path)
    if suffix == ".mvr":
        raise SystemExit(
            f"{path.name} is a whole rig, not a single fixture. "
            "Use 'lx rig' to summarise it and 'lx doctor' to check what it carries."
        )
    if suffix == ".hed":
        return chamsys.read(path)
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
                size = m.channel_count or "?"
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
    lib = _load_libraries(args)
    library = lib.fixtures

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
        where = libmod.label_for(m.fixture.source)
        print(f"{i:>2}. [{flag:>5}] {m.label}   <{where}>")
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


def _load_libraries(args: argparse.Namespace) -> "libmod.Library":
    """Load every library named on the command line, or auto-detect."""
    paths = list(getattr(args, "library", None) or [])
    if getattr(args, "folder", None):
        paths.append(args.folder)

    lib = libmod.load(paths or None,
                      include_ofl=getattr(args, "with_ofl", False),
                      cache=getattr(args, "cache", None))

    if not lib.fixtures:
        detected = libmod.detect_sources()
        hint = ("\n".join(f"  {p}" for p in detected)
                if detected else "  (none found automatically)")
        raise SystemExit(
            "No fixture libraries loaded. Point at one with --library, or use\n"
            f"a detected folder:\n{hint}"
        )

    counts = ", ".join(f"{n} from {src}" for src, n in lib.counts().items())
    print(f"Libraries: {counts}", file=sys.stderr)
    for err in lib.errors[:5]:
        print(f"  note: {err}", file=sys.stderr)
    return lib


def cmd_triage(args: argparse.Namespace) -> int:
    """Sort a folder of new fixtures into have / close / new."""
    folder = Path(args.inbox)
    files = [p for p in sorted(folder.iterdir())
             if p.suffix.lower() in {".gdtf", ".json", ".xml"}]
    if not files:
        print(f"no fixture files in {folder}", file=sys.stderr)
        return 1

    lib = _load_libraries(args)
    print(f"\nTriaging {len(files)} file(s) against {len(lib)} fixtures\n")

    have, close, new = [], [], []
    for path in files:
        try:
            fx = load_fixture(path)
        except (SystemExit, ValueError, OSError) as exc:
            print(f"  ?  {path.name}: {exc}")
            continue
        if not fx.modes:
            new.append((path.name, None, 0.0))
            continue

        best = matching.find_candidates(fx, fx.modes[0], lib.fixtures, limit=1)
        if not best:
            new.append((path.name, None, 0.0))
        elif best[0].score >= args.have_threshold:
            have.append((path.name, best[0], best[0].score))
        elif best[0].score >= args.close_threshold:
            close.append((path.name, best[0], best[0].score))
        else:
            new.append((path.name, best[0], best[0].score))

    def show(title: str, rows: list) -> None:
        print(f"{title} ({len(rows)})")
        for name, match, score in rows:
            if match is None:
                print(f"   {name}")
            else:
                where = libmod.label_for(match.fixture.source)
                print(f"   {name:<44} {score:.0%}  {match.label} <{where}>")
        print()

    show("ALREADY HAVE", have)
    show("CLOSE - usable with edits", close)
    show("NEW - need building", new)

    print(f"{len(have)} have, {len(close)} close, {len(new)} new")
    return 0


def cmd_gdtf(args: argparse.Namespace) -> int:
    """GDTF Share: log in, search, and download fixtures."""
    import getpass

    cache = getattr(args, "cache", None)

    if args.action == "login":
        user = args.user or input("GDTF Share email: ").strip()
        # Prompted, used once, never written to disk - only the session
        # cookie it returns is saved.
        password = getpass.getpass("GDTF Share password: ")
        gdtfshare.login(user, password, cache=cache)
        print("Logged in. Only the session cookie is stored, not your password.")
        return 0

    if args.action == "logout":
        print("Session removed." if gdtfshare.logout(cache) else "Not logged in.")
        return 0

    if not gdtfshare.logged_in(cache):
        raise SystemExit("Not logged in. Run 'lx gdtf login' first.")

    entries = gdtfshare.fetch_list(cache=cache)
    if args.query:
        entries = gdtfshare.search(entries, args.query)

    if args.action == "search":
        for e in entries[:args.limit]:
            rev = f"  rev {e.revision}" if e.revision else ""
            print(f"{e.rid:>7}  {e.label}{rev}")
        print(f"\n{len(entries)} match(es)")
        return 0

    # download
    if not entries:
        print("Nothing matches that query.", file=sys.stderr)
        return 1

    dest = Path(args.into)
    wanted = entries[:args.limit]
    print(f"Downloading {len(wanted)} of {len(entries)} match(es) into {dest}")
    ok = failed = 0
    for e in wanted:
        try:
            path = gdtfshare.download(e, dest, cache=cache, overwrite=args.overwrite)
            print(f"  {path.name}")
            ok += 1
        except gdtfshare.GdtfShareError as exc:
            print(f"  ! {e.label}: {exc}", file=sys.stderr)
            failed += 1
    print(f"\n{ok} downloaded, {failed} failed")
    print(f"Now: lx triage {dest}")
    return 0 if not failed else 1


def cmd_dupes(args: argparse.Namespace) -> int:
    """Find modes in a library that share a DMX fingerprint."""
    lib = _load_libraries(args)
    groups = libmod.find_duplicates(lib.fixtures, min_channels=args.min_channels)

    redundant = [g for g in groups if g.redundant()]
    alike = [g for g in groups if g.interchangeable()]
    covered = sum(g.size for g in groups)

    print(f"\n{len(groups)} group(s) of identical layout, covering {covered} modes")
    print("(a shared layout is normal - one fixture's effect modes all look "
          "alike)\n")

    if redundant:
        print(f"TRUE DUPLICATES ({len(redundant)}) - same fixture and mode stored twice")
        for g in redundant[:args.limit]:
            print(f"  {g.channel_count:>3} ch  x{g.size}  {g.names[0]}")
            for n in g.names[1:]:
                print(f"                 {n}")
        print()

    if alike:
        print(f"DIFFERENT FIXTURES, SAME LAYOUT ({len(alike)}) - interchangeable")
        for g in alike[:args.limit]:
            attrs = ", ".join(a for a, _ in g.signature[:6])
            print(f"  {g.channel_count:>3} ch  x{g.size}  {attrs}...")
            for n in g.names[:args.show]:
                print(f"                 {n}")
            if g.size > args.show:
                print(f"                 ... and {g.size - args.show} more")
        print()

    print(f"{len(redundant)} redundant, {len(alike)} interchangeable")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    if not args.source and not args.ofl:
        print("give a source file, or --ofl <key-or-search>", file=sys.stderr)
        return 1

    fixture = _resolve_target(args) if args.ofl else load_fixture(args.source)
    out = Path(args.dest)
    suffix = out.suffix.lower()

    chosen = None
    if getattr(args, "mode", None):
        wanted = args.mode.strip().lower()
        chosen = next((m for m in fixture.modes if m.name.strip().lower() == wanted), None)
        if chosen is None:
            available = ", ".join(m.name for m in fixture.modes) or "none"
            raise SystemExit(f"no mode called {args.mode!r}. Available: {available}")

    if suffix == ".gdtf":
        gdtf.write(fixture, out)
    elif suffix == ".xml":
        ma2.write(fixture, out)
    elif suffix == ".hed":
        chamsys.write(fixture, out, chosen)
        print("note: .hed output is desk-verified for layout, 16-bit, banks and "
              "homing; wheel slot-name display is not yet confirmed on a desk.",
              file=sys.stderr)
    else:
        raise SystemExit(
            f"cannot write {suffix or out.name}. Supported targets: .gdtf, .xml, .hed"
        )

    if suffix == ".hed":
        # A .hed holds one mode. Saying "2 mode(s)" here read as though both
        # had been written, when only the first ever was.
        written = chosen or (fixture.modes[0] if fixture.modes else None)
        label = f"{written.name} ({written.channel_count} ch)" if written else "no channels"
        print(f"{fixture.key}: wrote {label} -> {out}")
        others = [m.name for m in fixture.modes if m is not written]
        if others:
            print(f"  not written: {', '.join(others)}"
                  f"  (one .hed per mode; re-run with --mode)")
    else:
        modes = ", ".join(f"{m.name} ({m.channel_count} ch)" for m in fixture.modes)
        print(f"{fixture.key}: {len(fixture.modes)} mode(s) -> {out}")
        print(f"  {modes}")
    return 0



def _match_reference(fixture: Fixture) -> None:
    """Rank known fixtures against a drafted plan - "what is this really?"."""
    try:
        lib = libmod.load(None, include_ofl=True)
    except Exception as exc:      # noqa: BLE001 - advisory, never fatal
        print(f"  (could not load libraries to compare: {exc})", file=sys.stderr)
        return
    if not lib.fixtures:
        return
    mode = fixture.modes[0]
    hits = matching.find_candidates(fixture, mode, lib.fixtures, limit=5)
    if not hits:
        return
    print("\nClosest known fixtures - consider starting from one of these:")
    for i, m in enumerate(hits, 1):
        flag = "EXACT" if m.exact else f"{m.score:.0%}"
        print(f"  {i}. [{flag:>5}] {m.label}  <{libmod.label_for(m.fixture.source)}>")


def cmd_head(args: argparse.Namespace) -> int:
    """Make and tweak custom heads via editable plan files."""
    from . import chart, plan
    from .formats import chamsys as _ch

    if args.action == "template":
        if getattr(args, "blank", None):
            fixture = plan.blank(args.blank)
        elif getattr(args, "ofl", None):
            fixture = _resolve_target(args)
        elif args.source:
            fixture = load_fixture(args.source)
        else:
            raise SystemExit(
                "give a source file, --ofl <key>, or --blank <channels>")
        mode = fixture.mode(args.mode) if args.mode else None
        if args.mode and mode is None:
            available = ", ".join(m.name for m in fixture.modes) or "none"
            raise SystemExit(f"no mode called {args.mode!r}. Available: {available}")
        out = Path(args.out)
        out.write_text(plan.dump(fixture, mode), encoding="utf-8")
        print(f"plan written to {out} - edit it, then: lx head build {out}")
        return 0

    if args.action == "from-text":
        raw = (sys.stdin.read() if args.chart == "-"
               else Path(args.chart).read_text(encoding="utf-8", errors="replace"))
        fixture = chart.parse_chart(raw)
        out = Path(args.out or "head-plan.txt")
        out.write_text(plan.dump(fixture), encoding="utf-8")
        n = len(fixture.modes[0].channels)
        print(f"recognised {n} channel(s) -> {out}")
        if fixture.source_id:
            print(f"  could not read: {fixture.source_id}")
        for w in plan.warnings(fixture):
            print(f"  warning: {w}")
        if getattr(args, "match", False):
            _match_reference(fixture)
        print(f"CHECK THE PLAN against the manual, then: lx head build {out}")
        return 0

    # build
    fixture = plan.parse(Path(args.plan).read_text(encoding="utf-8"))
    mode = fixture.modes[0]
    if args.out:
        out = Path(args.out)
    else:
        stem = f"{fixture.manufacturer}_{fixture.model}_{mode.name}".strip("_")
        out = Path(re.sub(r"[^A-Za-z0-9 ._+-]", "", stem) + ".hed")
    for w in plan.warnings(fixture):
        print(f"warning: {w}", file=sys.stderr)
    _ch.write(fixture, out, mode)
    print(f"{fixture.key}: wrote {mode.name} ({mode.channel_count} ch) -> {out}")
    if getattr(args, "save", False):
        from . import mylib
        saved = mylib.save(fixture, mode, plan_text=Path(args.plan).read_text())
        print(f"saved to your library: {saved.hed}")
    print("copy it into the MagicQ heads folder and restart MagicQ")
    return 0


def cmd_heads(args: argparse.Namespace) -> int:
    """List, reopen and remove your saved custom heads."""
    from . import mylib, plan

    if args.action == "list":
        rows = mylib.entries()
        if not rows:
            print(f"no saved heads yet (they live in {mylib.store_dir()})")
            print("save one with:  lx head build plan.txt --save")
            return 0
        print(f"{len(rows)} saved head(s) in {mylib.store_dir()}:\n")
        for h in rows:
            print(f"  {h.stem:<40} {h.manufacturer} {h.model} [{h.mode}] {h.channels}ch")
        return 0

    if args.action == "edit":
        text = mylib.get_plan(args.name)
        if text is None:
            raise SystemExit(f"no saved head called {args.name!r} (see 'lx heads list')")
        out = Path(args.out or f"{args.name}.plan")
        out.write_text(text, encoding="utf-8")
        print(f"plan written to {out} - edit it, then: lx head build {out} --save")
        return 0

    if args.action == "remove":
        if mylib.remove(args.name):
            print(f"removed {args.name}")
            return 0
        raise SystemExit(f"no saved head called {args.name!r}")

    return 1


def cmd_rig(args: argparse.Namespace) -> int:
    """Summarise a whole patch from an MVR, and flag address clashes."""
    rig = mvr.read(args.file)
    types = rig.types()
    print(f"{rig.name}: {len(rig.fixtures)} fixture(s), {len(types)} type(s)\n")

    for universe, members in rig.by_universe().items():
        print(f"Universe {universe}:")
        for pf in members:
            span = f"{pf.address}-{pf.last_address}" if pf.footprint > 1 else str(pf.address)
            unknown = "" if pf.fixture.modes else "   [type not embedded]"
            print(f"  {span:>9}  {pf.name:<24} {pf.fixture.key} [{pf.mode}]{unknown}")
        print()

    clashes = rig.conflicts()
    if clashes:
        print(f"{len(clashes)} address clash(es):")
        for a, b in clashes:
            print(f"  U{a.universe} {a.name} ({a.address}-{a.last_address}) "
                  f"overlaps {b.name} ({b.address}-{b.last_address})")
    else:
        print("No address clashes.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what we can and cannot read from a file - no guessing."""
    path = Path(args.file)

    if path.suffix.lower() == ".mvr":
        rig = mvr.read(path)
        missing = [pf for pf in rig.fixtures if not pf.fixture.modes]
        print(f"{path.name}: {len(rig.fixtures)} fixture(s), {len(rig.types())} type(s)")
        print(f"  universes : {', '.join(str(u) for u in rig.by_universe())}")
        print(f"  types embedded: {len(rig.types()) - len({p.fixture.key for p in missing})}"
              f" of {len(rig.types())}")
        if missing:
            names = sorted({p.fixture.source_id for p in missing})
            print(f"  referenced but not embedded: {', '.join(names)}")
        print(f"  address clashes: {len(rig.conflicts())}")
        return 0

    data = path.read_bytes()

    if path.suffix.lower() == ".hed":
        obf = chamsys.looks_obfuscated(data)
        head = chamsys.parse_head_filename(path)
        print(f"{path.name}")
        print(f"  body      : {'obfuscated (decoded ok)' if obf else 'plain text'}")
        print(f"  from name : {head.manufacturer!r} / {head.model!r} / {head.mode!r}")
        if args.verbose:
            print()
            print(chamsys.decode_hed(data))
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
    m.add_argument("folder", nargs="?", help="a library folder (omit to auto-detect)")
    m.add_argument("--library", action="append", metavar="PATH",
                   help="extra library folder; repeatable")
    m.add_argument("--with-ofl", action="store_true",
                   help="also search the cached Open Fixture Library")
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
    c.add_argument("source", nargs="?", help="GDTF / OFL JSON / MA2 / MA3 XML / .hed")
    c.add_argument("dest", help="output .gdtf, .xml or .hed")
    c.add_argument("--mode", metavar="MODE",
                   help="which mode to write (a .hed holds exactly one)")
    c.add_argument("--ofl", metavar="KEY",
                   help="convert a fixture from the OFL catalogue instead of a file")
    c.add_argument("--cache", help="catalogue cache directory")
    c.set_defaults(func=cmd_convert)

    t = sub.add_parser("triage", help="sort a folder of fixtures into have/close/new")
    t.add_argument("inbox", help="folder of .gdtf / .json / .xml fixtures to sort")
    t.add_argument("--library", action="append", metavar="PATH",
                   help="library folder to check against; repeatable")
    t.add_argument("--with-ofl", action="store_true")
    t.add_argument("--have-threshold", type=float, default=0.90)
    t.add_argument("--close-threshold", type=float, default=0.60)
    t.add_argument("--cache", help="catalogue cache directory")
    t.set_defaults(func=cmd_triage)

    g = sub.add_parser("gdtf", help="GDTF Share: log in, search, download")
    g.add_argument("action", choices=["login", "logout", "search", "download"])
    g.add_argument("query", nargs="?", default="", help="free-text filter")
    g.add_argument("--user", help="account email (prompted if omitted)")
    g.add_argument("--into", default="gdtf-share",
                   help="download folder (default ./gdtf-share)")
    g.add_argument("--limit", type=int, default=50)
    g.add_argument("--overwrite", action="store_true")
    g.add_argument("--cache")
    g.set_defaults(func=cmd_gdtf)

    d2 = sub.add_parser("dupes", help="find fixtures with identical channel layouts")
    d2.add_argument("folder", nargs="?", help="library folder (omit to auto-detect)")
    d2.add_argument("--library", action="append", metavar="PATH")
    d2.add_argument("--with-ofl", action="store_true")
    d2.add_argument("--min-channels", type=int, default=4,
                    help="ignore modes smaller than this (default 4)")
    d2.add_argument("--limit", type=int, default=15, help="groups to print")
    d2.add_argument("--show", type=int, default=4, help="members to print per group")
    d2.add_argument("--cache")
    d2.set_defaults(func=cmd_dupes)

    hd = sub.add_parser("head", help="make or tweak a custom head from an editable plan")
    hsub = hd.add_subparsers(dest="action", required=True)
    ht = hsub.add_parser("template", help="write an editable plan from a reference fixture")
    ht.add_argument("out", help="plan file to write, e.g. plan.txt")
    ht.add_argument("source", nargs="?", help="GDTF / OFL JSON / MA2 XML / .hed reference")
    ht.add_argument("--ofl", metavar="KEY", help="use a catalogue fixture as the reference")
    ht.add_argument("--blank", type=int, metavar="N", help="start from N empty channels")
    ht.add_argument("--mode", help="which mode of the reference to start from")
    ht.add_argument("--cache", help="catalogue cache directory")
    ht.set_defaults(func=cmd_head)
    hf = hsub.add_parser("from-text", help="draft a plan from a pasted DMX chart (manual/screenshot text)")
    hf.add_argument("chart", help="text file with the chart, or - for stdin")
    hf.add_argument("-o", "--out", help="plan file to write (default head-plan.txt)")
    hf.add_argument("--match", action="store_true",
                    help="also rank known fixtures against the chart ('what is this really?')")
    hf.set_defaults(func=cmd_head)
    hb = hsub.add_parser("build", help="compile a plan into a MagicQ .hed")
    hb.add_argument("plan", help="the edited plan file")
    hb.add_argument("out", nargs="?", help="output .hed (default from manufacturer/model/mode)")
    hb.add_argument("--save", action="store_true",
                    help="also save into your personal head library for next time")
    hb.set_defaults(func=cmd_head)

    he = sub.add_parser("heads", help="list, reopen and remove your saved custom heads")
    hesub = he.add_subparsers(dest="action", required=True)
    hel = hesub.add_parser("list", help="list saved heads")
    hel.set_defaults(func=cmd_heads)
    hee = hesub.add_parser("edit", help="write a saved head's plan back out to edit")
    hee.add_argument("name", help="the saved head's name (from 'lx heads list')")
    hee.add_argument("-o", "--out", help="plan file to write")
    hee.set_defaults(func=cmd_heads)
    her = hesub.add_parser("remove", help="delete a saved head")
    her.add_argument("name", help="the saved head's name")
    her.set_defaults(func=cmd_heads)

    r = sub.add_parser("rig", help="summarise a whole patch from an MVR file")
    r.add_argument("file")
    r.set_defaults(func=cmd_rig)

    d = sub.add_parser("doctor", help="report what can be read from a file")
    d.add_argument("file")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(func=cmd_doctor)

    return p


def _heads_folder_hint(folder: str) -> str:
    """Help someone who pointed at the wrong place find their heads folder."""
    return (
        f"Could not find a heads folder at:\n  {Path(folder).expanduser()}\n\n"
        "MagicQ keeps personalities under its show directory, as\n"
        "<MagicQ folder>/show/heads - on macOS usually\n"
        "~/Documents/MagicQ/show/heads. If that is not it, search for them:\n\n"
        "  macOS/Linux:  find ~ /Applications -name '*.hed' 2>/dev/null | head -5\n"
        "  Windows:      dir /s /b C:\\*.hed\n\n"
        "Then pass the folder those .hed files live in."
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (NotADirectoryError, FileNotFoundError) as exc:
        folder = getattr(args, "folder", None)
        if folder and str(folder) in str(exc):
            print(_heads_folder_hint(folder), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        # Malformed fixture files are the user's problem to fix, not a bug to
        # dump a traceback for.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
