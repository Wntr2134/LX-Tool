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
