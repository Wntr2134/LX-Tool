"""Run the X-Touch <-> grandMA3 bridge against real ports.

This is the only module in the package that touches hardware; everything
above it is pure and tested. MIDI needs the optional `mido` +
`python-rtmidi` pair (pip install "lx-tool[xtouch]"); OSC is a plain UDP
socket.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from dataclasses import fields
from pathlib import Path

from .bridge import Bridge, Config


def load_config(path: str | Path | None) -> Config:
    """A Config from JSON, tolerating absent file and unknown keys."""
    cfg = Config()
    if not path:
        return cfg
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {f.name for f in fields(Config)}
    for k, v in data.items():
        if k not in known:
            print(f"config: ignoring unknown key {k!r}", file=sys.stderr)
            continue
        if isinstance(getattr(cfg, k), tuple) and isinstance(v, list):
            v = tuple(v)
        setattr(cfg, k, v)
    return cfg


def default_config_json() -> str:
    """A commented-enough starting config to hand to a user."""
    cfg = Config()
    body = {f.name: (list(v) if isinstance(v := getattr(cfg, f.name), tuple)
                     else v)
            for f in fields(Config)}
    return json.dumps(body, indent=2) + "\n"


def find_xtouch_port(names: list[str]) -> str | None:
    """The first MIDI port that looks like an X-Touch."""
    for n in names:
        if "x-touch" in n.lower() or "xtouch" in n.lower():
            return n
    return None


def run(*, ma3_host: str = "127.0.0.1", send_port: int = 8000,
        recv_port: int = 9000, midi_port: str = "",
        config_path: str = "") -> int:
    """Bridge until Ctrl-C. Returns an exit code."""
    try:
        import mido
    except ImportError:
        print('MIDI support is not installed. Run:  pip install "lx-tool[xtouch]"',
              file=sys.stderr)
        return 1

    names = mido.get_input_names()
    port_name = midi_port or find_xtouch_port(names)
    if not port_name:
        print("no X-Touch found. MIDI inputs seen:", file=sys.stderr)
        for n in names or ["(none)"]:
            print(f"  {n}", file=sys.stderr)
        print("plug the X-Touch in via USB, set it to MC mode "
              "(hold SELECT ch1 while powering on), or pass --midi-port",
              file=sys.stderr)
        return 1

    bridge = Bridge(config=load_config(config_path or None))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", recv_port))
    sock.setblocking(False)
    ma3 = (ma3_host, send_port)

    with mido.open_input(port_name) as midi_in, \
            mido.open_output(port_name) as midi_out:
        print(f"X-Touch:  {port_name}")
        print(f"MA3:      sending to {ma3_host}:{send_port}, "
              f"listening on :{recv_port}")
        print("MA3 setup: Menu > In & Out > OSC - destination this machine, "
              "Send+Receive on, executor feedback on")
        for raw in bridge.hello():
            midi_out.send(mido.Message.from_bytes(raw))
        print("bridging - Ctrl-C to stop")

        try:
            while True:
                worked = False
                for msg in midi_in.iter_pending():
                    worked = True
                    for datagram in bridge.midi_in(bytes(msg.bytes())):
                        sock.sendto(datagram, ma3)
                while True:
                    try:
                        datagram, _ = sock.recvfrom(4096)
                    except BlockingIOError:
                        break
                    worked = True
                    for raw in bridge.osc_in(datagram):
                        midi_out.send(mido.Message.from_bytes(raw))
                if not worked:
                    time.sleep(0.002)
        except KeyboardInterrupt:
            print("\nstopping")
            return 0


def selftest(midi_port: str = "") -> int:
    """Wiggle the surface: proves MIDI out and MC mode without MA3."""
    try:
        import mido
    except ImportError:
        print('MIDI support is not installed. Run:  pip install "lx-tool[xtouch]"',
              file=sys.stderr)
        return 1
    from . import mcu

    names = mido.get_output_names()
    port_name = midi_port or find_xtouch_port(names)
    if not port_name:
        print("no X-Touch found among MIDI outputs:", file=sys.stderr)
        for n in names or ["(none)"]:
            print(f"  {n}", file=sys.stderr)
        return 1

    with mido.open_output(port_name) as out:
        print(f"testing {port_name}: faders sweep, rings light, "
              "strips say hello")
        for raw in (mcu.lcd_text(s, 0, "LX-Tool") for s in range(8)):
            out.send(mido.Message.from_bytes(raw))
        for step in range(0, 11):
            for s in range(9):
                out.send(mido.Message.from_bytes(
                    mcu.fader_out(s, step / 10)))
            for e in range(8):
                out.send(mido.Message.from_bytes(
                    mcu.encoder_ring(e, step / 10, mode=2)))
            time.sleep(0.12)
        for raw in mcu.blank_surface():
            out.send(mido.Message.from_bytes(raw))
    print("done - if the faders moved, MC mode and cabling are good")
    return 0
