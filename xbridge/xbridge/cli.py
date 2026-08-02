"""XBridge command line: run, test, sniff, config, web."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import run as xrun


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="xbridge",
        description="Behringer X-Touch as a native surface for lighting, "
                    "media and show control")
    sub = p.add_subparsers(dest="action", required=True)

    r = sub.add_parser("run", help="start the bridge")
    r.add_argument("--target", default="",
                   choices=("", "ma3", "ma2", "magicq", "eos", "x32",
                            "resolume", "companion", "generic"))
    r.add_argument("--host", default="127.0.0.1",
                   help="machine running the console (default: this one)")
    r.add_argument("--send-port", type=int, default=0,
                   help="console's OSC port (default: per target)")
    r.add_argument("--recv-port", type=int, default=9000)
    r.add_argument("--midi-port", default="")
    r.add_argument("--config", default="",
                   help="mapping file (default: the app's stored mapping)")

    t = sub.add_parser("test", help="wiggle the surface: prove MIDI and MC mode")
    t.add_argument("--midi-port", default="")

    s = sub.add_parser("sniff", help="print everything both sides say, decoded")
    s.add_argument("--recv-port", type=int, default=9000)
    s.add_argument("--midi-port", default="")
    s.add_argument("--seconds", type=float, default=30.0)

    sub.add_parser("ports", help="list the MIDI ports this machine can see")

    ms = sub.add_parser("ma3-setup",
                        help="print the OSC lines to create on the console")
    ms.add_argument("--host", default="127.0.0.1")
    ms.add_argument("--send-port", type=int, default=8000)
    ms.add_argument("--recv-port", type=int, default=9000)
    ms.add_argument("--prefix", default="")
    ms.add_argument("--bridge-ip", default="127.0.0.1",
                    help="the machine running the bridge, as the console "
                         "should address it")

    pb = sub.add_parser("probe", help="find the OSC format your MA3 wants")
    pb.add_argument("--host", default="127.0.0.1")
    pb.add_argument("--port", type=int, default=8000)
    pb.add_argument("--page", type=int, default=1)
    pb.add_argument("--exec", dest="exec_no", type=int, default=201)
    pb.add_argument("--dwell", type=float, default=2.0,
                    help="seconds between steps (default: 2)")

    c = sub.add_parser("config", help="write the default mapping to edit")
    c.add_argument("-o", "--out", help="file to write (default: print)")

    w = sub.add_parser("web", help="serve the panel to a browser")
    w.add_argument("--port", type=int, default=8765)

    args = p.parse_args(argv)

    if args.action == "run":
        config = args.config
        if not config and xrun.config_store_path().is_file():
            config = str(xrun.config_store_path())
        return xrun.run(ma3_host=args.host, send_port=args.send_port,
                        recv_port=args.recv_port, midi_port=args.midi_port,
                        config_path=config, target=args.target)
    if args.action == "test":
        return xrun.selftest(midi_port=args.midi_port)
    if args.action == "sniff":
        return xrun.sniff(recv_port=args.recv_port, midi_port=args.midi_port,
                          seconds=args.seconds)
    if args.action == "ports":
        if not xrun.midi_available():
            print("MIDI support is not installed: pip install mido python-rtmidi")
            return 1
        import mido

        try:
            ins, outs = mido.get_input_names(), mido.get_output_names()
        except Exception as exc:  # noqa: BLE001 - no MIDI system at all
            print(f"the MIDI system is unavailable: {exc}")
            return 1
        print("MIDI inputs:")
        for n in ins or ["  (none)"]:
            print(f"  {n}")
        print("MIDI outputs:")
        for n in outs or ["  (none)"]:
            print(f"  {n}")
        for name in ("xtouch", "mpk"):
            found = xrun.find_surface_port(ins, name)
            if found:
                pair = (xrun.find_surface_port(outs, name)
                        or xrun._matching_port(found, outs))
                print(f"\n{name}: in={found!r} out={pair or 'NONE'!r}")
        return 0

    if args.action == "ma3-setup":
        from . import ma3setup

        print("grandMA3: Menu - In & Out - OSC\n")
        print("An OSC line has ONE Port cell, used for both directions.")
        print("There is no separate send and receive port - which is why a")
        print("round trip needs two lines.\n")
        print("Top of the menu:")
        for name, value, note in ma3setup.GLOBAL_TOGGLES:
            print(f"  {name:<20} {value}")
            if note:
                print(f"  {'':<20} ({note})")
        for ln in ma3setup.lines(host=args.host, send_port=args.send_port,
                                 recv_port=args.recv_port,
                                 prefix=args.prefix,
                                 bridge_ip=args.bridge_ip):
            print(f"\n{ln.title}\n  {ln.why}")
            for name, value, note in ln.cells:
                print(f"  {name:<20} {value}")
                if note:
                    print(f"  {'':<20} ({note})")
        warn = ma3setup.warnings(host=args.host, send_port=args.send_port,
                                 recv_port=args.recv_port)
        for w in warn:
            print(f"\nWatch out: {w}")
        print(f"\n{ma3setup.feedback_note()}")
        return 0

    if args.action == "probe":
        from .probe import Ma3Probe

        p = Ma3Probe(host=args.host, port=args.port, page=args.page,
                     exec_=args.exec_no)
        print(f"Watch executor {args.exec_no} on page {args.page}. "
              f"Sending to {args.host}:{args.port}, "
              f"{args.dwell:g}s apart:\n")
        try:
            p.run(dwell=args.dwell,
                  on_step=lambda s: print(f"  {s.label:<32} {s.line}"))
        except OSError as exc:
            print(f"could not send: {exc}")
            return 1
        print("\nWhichever step moved the fader is your format. Set it with:"
              "\n  xbridge config -o mapping.json   (edit prefix + ma3_value)"
              "\nor press Keep on that row in the app's 'Find MA3 format'.")
        return 0

    if args.action == "config":
        text = xrun.default_config_json()
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {args.out} - edit it, then: xbridge run --config {args.out}")
        else:
            print(text, end="")
        return 0
    # web
    import uvicorn

    from .app import app

    print(f"XBridge panel: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
