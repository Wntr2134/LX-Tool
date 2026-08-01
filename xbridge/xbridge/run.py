"""Run the X-Touch <-> grandMA3 bridge against real ports.

This is the only module in the package that touches hardware; everything
above it is pure and tested. MIDI needs the optional `mido` +
`python-rtmidi` pair (pip install mido python-rtmidi); OSC is a plain UDP
socket.

The Runner survives the real world: X-Touch unplugged mid-show, MA3
restarted, the bridge started before either is ready. It keeps retrying
rather than dying, and exposes its state so a UI can show what's going on.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
from dataclasses import fields
from pathlib import Path

from . import mcu, osc
from .bridge import Bridge, Config

_RETRY_SECS = 2.0


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
    """A starting config to hand to a user."""
    cfg = Config()
    body = {f.name: (list(v) if isinstance(v := getattr(cfg, f.name), tuple)
                     else v)
            for f in fields(Config)}
    return json.dumps(body, indent=2) + "\n"


def find_xtouch_port(names: list[str]) -> str | None:
    """The first MIDI port that looks like an X-Touch."""
    return find_surface_port(names, "xtouch")


_PORT_HINTS = {"xtouch": ("x-touch", "xtouch"), "mpk": ("mpk",)}


def find_surface_port(names: list[str], surface: str) -> str | None:
    """The first MIDI port that looks like the configured surface."""
    hints = _PORT_HINTS.get(surface, ("x-touch", "xtouch"))
    for n in names:
        low = n.lower()
        if any(h in low for h in hints):
            return n
    return None


def _port_names(mido, direction: str) -> list:
    """Input or output port names, or [] if the backend cannot say."""
    try:
        if direction == "output":
            return list(mido.get_output_names())
        return list(mido.get_input_names())
    except Exception:      # noqa: BLE001 - a backend hiccup is not fatal here
        return []


def _base_name(name: str) -> str:
    """A port name without the index the backend appends.

    Windows enumerates "X-Touch 0" on the input side and "X-Touch 1" on
    the output side for one physical device, so the trailing number is
    exactly what must be ignored when pairing them up.
    """
    return re.sub(r"\s+\d+$", "", (name or "").strip()).lower()


def _matching_port(name: str, candidates: list) -> str:
    """The candidate that is the same device as `name`, index aside."""
    base = _base_name(name)
    if not base:
        return ""
    for c in candidates:
        if _base_name(c) == base:
            return c
    for c in candidates:                      # last resort: a prefix match
        if base and _base_name(c).startswith(base[:8]):
            return c
    return ""


def _open_pair(mido, in_name: str, out_name: str):
    """Open input and output together, or leave nothing open.

    Without the cleanup an output failure strands the input handle, and
    since Windows MIDI ports are exclusive-access the reconnect loop
    would make the device progressively harder to open rather than
    recovering.
    """
    midi_in = mido.open_input(in_name)
    if not out_name:
        return midi_in, None
    try:
        return midi_in, mido.open_output(out_name)
    except Exception:
        try:
            midi_in.close()
        except Exception:      # noqa: BLE001
            pass
        raise


def midi_available() -> bool:
    try:
        import mido  # noqa: F401
        return True
    except ImportError:
        return False


class Runner:
    """The bridge against real ports, with reconnect and visible state.

    state is one of: "starting", "waiting-for-surface", "running",
    "stopped", "error". A UI can poll state/detail/counters while the
    run() loop owns the thread it was started on.
    """

    def __init__(self, *, ma3_host: str = "127.0.0.1", send_port: int = 0,
                 recv_port: int = 9000, midi_port: str = "",
                 config_path: str = "", target: str = "", surface: str = "",
                 log=print):
        cfg = load_config(config_path or None)
        if target:
            cfg.target = target
        if surface:
            cfg.surface = surface
        self.bridge = Bridge(config=cfg)
        # port 0 = "the target's usual port": 8000 for MA3, 10023 for X32.
        if not send_port:
            send_port = getattr(self.bridge.target, "default_send_port", 8000)
        self.ma3 = (ma3_host, send_port)
        self.recv_port = recv_port
        self.midi_port = midi_port
        self.stop_event = threading.Event()
        self.state = "starting"
        self.detail = ""
        self.midi_name = ""
        self.counters = {"midi_in": 0, "osc_in": 0}
        self._log = log

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> int:
        """Bridge until stopped. Reconnects instead of dying."""
        try:
            import mido
        except ImportError:
            self.state, self.detail = "error", (
                'MIDI support is not installed. Run:  '
                'pip install mido python-rtmidi')
            self._log(self.detail)
            return 1

        if getattr(self.bridge.target, "transport", "osc") == "ma2ws":
            return self._run_ma2ws(mido)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", self.recv_port))
        except OSError as exc:
            self.state, self.detail = "error", (
                f"cannot listen on UDP {self.recv_port}: {exc} "
                "(is another bridge already running?)")
            self._log(self.detail)
            return 1
        sock.setblocking(False)

        try:
            while not self.stop_event.is_set():
                try:
                    names = mido.get_input_names()
                except Exception as exc:  # noqa: BLE001 - backend won't init
                    self.state, self.detail = "error", (
                        f"the MIDI system is unavailable: {exc}")
                    self._log(self.detail)
                    return 1
                port_name = self.midi_port or next(
                    (find_surface_port(names, s.name)
                     for s in self.bridge.surfaces
                     if find_surface_port(names, s.name)), None)
                if not port_name:
                    if self.state != "waiting-for-surface":
                        self.state = "waiting-for-surface"
                        seen = ", ".join(names) or "none"
                        self.detail = ("no surface on USB (X-Touch: MC mode, "
                                       f"USB). MIDI inputs seen: {seen}")
                        self._log(self.detail)
                    if self.stop_event.wait(_RETRY_SECS):
                        break
                    continue
                try:
                    self._session(mido, port_name, sock)
                except KeyboardInterrupt:
                    break
                except Exception as exc:  # noqa: BLE001 - reconnect, don't die
                    self.state = "waiting-for-surface"
                    self.detail = f"lost {port_name}: {exc} - reconnecting"
                    self._log(self.detail)
                    if self.stop_event.wait(_RETRY_SECS):
                        break
        except KeyboardInterrupt:
            pass
        finally:
            sock.close()
        self.state, self.detail = "stopped", ""
        self._log("bridge stopped")
        return 0

    def _open_surfaces(self, mido, names) -> list:
        """(surface, midi_in, midi_out, name) for every surface present.

        The input and output names are resolved against their OWN lists:
        Windows hands the same device different names in each ("X-Touch 0"
        in, "X-Touch 1" out), so opening the output with the input's name
        fails with "unknown port". A surface that isn't plugged in yet is
        skipped and attached later.
        """
        outs = _port_names(mido, "output")
        conns = []
        for surface in self.bridge.surfaces:
            want = (self.midi_port
                    if self.midi_port and len(self.bridge.surfaces) == 1
                    else "")
            in_name = want or find_surface_port(names, surface.name)
            if not in_name or any(in_name == c[3] for c in conns):
                continue
            out_name = (find_surface_port(outs, surface.name)
                        or _matching_port(in_name, outs))
            try:
                pair = _open_pair(mido, in_name, out_name)
            except Exception as exc:  # noqa: BLE001 - half-plugged: try later
                self._log(f"could not open {in_name!r}: {exc}")
                continue
            conns.append((surface, pair[0], pair[1], in_name))
        return conns

    def _session(self, mido, port_name: str, sock) -> None:
        """One connected stretch: from first port open until stop/error."""
        conns = self._open_surfaces(mido, mido.get_input_names())
        if not conns:
            raise OSError("surface disappeared before the session opened")
        try:
            self.midi_name = ", ".join(c[3] for c in conns)
            self.state = "running"
            self.detail = (f"{self.midi_name} <-> {self.ma3[0]}:{self.ma3[1]}"
                           f" (feedback on :{self.recv_port})")
            self._log(f"Surfaces: {self.midi_name}")
            self._log(f"Console:  sending to {self.ma3[0]}:{self.ma3[1]}, "
                      f"listening on :{self.recv_port}")
            for surface, _mi, mout, _p in conns:
                if mout is None:
                    continue
                for raw in surface.hello(
                        self.bridge.target.strip_labels(
                            self.bridge.config.page)):
                    mout.send(mido.Message.from_bytes(raw))
            for datagram in self.bridge.osc_hello():
                sock.sendto(datagram, self.ma3)

            page = self.bridge.config.page
            last_rescan = time.monotonic()
            while not self.stop_event.is_set():
                worked = False
                for surface, mi, _mo, _p in conns:
                    for msg in mi.iter_pending():
                        worked = True
                        self.counters["midi_in"] += 1
                        for datagram in self.bridge.midi_in(
                                bytes(msg.bytes()), surface=surface):
                            sock.sendto(datagram, self.ma3)
                if self.bridge.config.page != page:
                    page = self.bridge.config.page
                    for surface, _mi, mout, _p in conns:
                        if mout is None:
                            continue
                        for s, ln, text in self.bridge.target.strip_labels(page):
                            from . import targets as _t
                            for raw in surface.render(
                                    _t.LabelFB(s, ln, text), _t):
                                mout.send(mido.Message.from_bytes(raw))
                while True:
                    try:
                        datagram, _ = sock.recvfrom(4096)
                    except BlockingIOError:
                        break
                    except OSError:
                        return
                    worked = True
                    self.counters["osc_in"] += 1
                    ctl = osc.decode(datagram)
                    if ctl is not None and ctl.address.startswith("/xbridge"):
                        for out_d in self.bridge.control_in(ctl):
                            if isinstance(out_d, bytes):
                                sock.sendto(out_d, self.ma3)
                        continue
                    msg = osc.decode(datagram)
                    intents = (self.bridge.target.feedback(msg)
                               if msg is not None else [])
                    for surface, _mi, mout, _p in conns:
                        if mout is None:
                            continue
                        for raw in self.bridge.render_for(surface, intents):
                            mout.send(mido.Message.from_bytes(raw))
                for datagram in self.bridge.tick(time.monotonic()):
                    sock.sendto(datagram, self.ma3)
                # A configured surface plugged in mid-session joins live.
                if (len(conns) < len(self.bridge.surfaces)
                        and time.monotonic() - last_rescan > 3.0):
                    last_rescan = time.monotonic()
                    have = {c[3] for c in conns}
                    for c in self._open_surfaces(mido, mido.get_input_names()):
                        if c[3] not in have:
                            conns.append(c)
                            self.midi_name = ", ".join(x[3] for x in conns)
                            self.detail = (f"{self.midi_name} <-> "
                                           f"{self.ma3[0]}:{self.ma3[1]}")
                            for raw in c[0].hello(
                                    self.bridge.target.strip_labels(page)):
                                c[2].send(mido.Message.from_bytes(raw))
                if not worked:
                    time.sleep(0.002)
        finally:
            for _s, mi, mout, _p in conns:
                for handle in (mi, mout):
                    try:
                        if handle is not None:
                            handle.close()
                    except Exception:  # noqa: BLE001
                        pass


    # ---- grandMA2 web remote (websocket, not OSC) ------------------------

    def _run_ma2ws(self, mido) -> int:
        """MA2 session loop: websocket to the console's web server.

        Same survival rules as the OSC loop: wait for the surface, wait
        for the console, reconnect on loss, stop cleanly.
        """
        try:
            from websockets.sync.client import connect
        except ImportError:
            self.state, self.detail = "error", (
                "MA2 support needs the websockets package: "
                "pip install websockets")
            self._log(self.detail)
            return 1

        while not self.stop_event.is_set():
            try:
                names = mido.get_input_names()
            except Exception as exc:  # noqa: BLE001
                self.state, self.detail = "error", (
                    f"the MIDI system is unavailable: {exc}")
                self._log(self.detail)
                return 1
            port_name = self.midi_port or find_surface_port(
                names, self.bridge.config.surface)
            if not port_name:
                if self.state != "waiting-for-surface":
                    self.state = "waiting-for-surface"
                    self.detail = "no surface on USB - waiting"
                    self._log(self.detail)
                if self.stop_event.wait(_RETRY_SECS):
                    break
                continue
            try:
                self._ma2_session(mido, port_name, connect)
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001 - reconnect, don't die
                self.state = "waiting-for-surface"
                self.detail = f"MA2 link lost: {exc} - reconnecting"
                self._log(self.detail)
                if self.stop_event.wait(_RETRY_SECS):
                    break
        self.state, self.detail = "stopped", ""
        self._log("bridge stopped")
        return 0

    def _ma2_session(self, mido, port_name: str, connect) -> None:
        import hashlib
        import json as jsonlib

        cfg = self.bridge.config
        url = f"ws://{self.ma3[0]}:{self.ma3[1]}/"
        target = self.bridge.target

        with mido.open_input(port_name) as midi_in, \
                mido.open_output(port_name) as midi_out, \
                connect(url, open_timeout=5) as ws:
            self.midi_name = port_name
            self.state = "running"
            self.detail = f"{port_name} <-> MA2 web remote {url}"
            self._log(self.detail)
            for raw in self.bridge.hello():
                midi_out.send(mido.Message.from_bytes(raw))

            session = 0
            logged_in = False
            last_keepalive = last_poll = 0.0

            def send(obj: dict) -> None:
                ws.send(jsonlib.dumps(obj))

            send({"session": 0})
            while not self.stop_event.is_set():
                now = time.monotonic()
                # ---- console -> surface
                try:
                    raw_msg = ws.recv(timeout=0.02)
                except TimeoutError:
                    raw_msg = None
                if raw_msg is not None:
                    self.counters["osc_in"] += 1
                    try:
                        msg = jsonlib.loads(raw_msg)
                    except ValueError:
                        msg = None
                    if isinstance(msg, dict):
                        new_session = msg.get("session")
                        if isinstance(new_session, int) and new_session > 0 \
                                and new_session != session:
                            session = new_session
                            logged_in = False
                        if session and not logged_in:
                            pw = hashlib.md5(
                                cfg.ma2_password.encode()).hexdigest()
                            send({"requestType": "login",
                                  "username": cfg.ma2_user,
                                  "password": pw, "session": session,
                                  "maxRequests": 10})
                            logged_in = True
                            self.detail = (f"{port_name} <-> MA2 {url} "
                                           f"(session {session})")
                        for raw in self.bridge.apply_feedback(
                                target.ws_feedback(msg)):
                            midi_out.send(mido.Message.from_bytes(raw))
                # ---- surface -> console
                for m in midi_in.iter_pending():
                    self.counters["midi_in"] += 1
                    for cmd in self.bridge.midi_in(bytes(m.bytes())):
                        if isinstance(cmd, str) and session:
                            send({"command": cmd, "session": session,
                                  "requestType": "command",
                                  "maxRequests": 0})
                # ---- keepalive + playback polling
                if session and now - last_keepalive > 5.0:
                    last_keepalive = now
                    send({"session": session})
                if session and logged_in and now - last_poll > 0.4:
                    last_poll = now
                    send(target.playbacks_request(session))


def run(*, ma3_host: str = "127.0.0.1", send_port: int = 0,
        recv_port: int = 9000, midi_port: str = "",
        config_path: str = "", target: str = "") -> int:
    """CLI entry: bridge until Ctrl-C."""
    runner = Runner(ma3_host=ma3_host, send_port=send_port,
                    recv_port=recv_port, midi_port=midi_port,
                    config_path=config_path, target=target)
    print("bridging - Ctrl-C to stop")
    return runner.run()


def config_store_path() -> Path:
    """Where the app keeps the surface mapping between runs."""
    import os

    env = os.environ.get("XBRIDGE_CONFIG")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "xbridge" / "config.json"


def load_stored_config() -> Config:
    p = config_store_path()
    return load_config(p) if p.is_file() else Config()


def store_config(data: dict) -> Config:
    """Validate by round-tripping through Config, then persist."""
    cfg = Config()
    known = {f.name for f in fields(Config)}
    clean = {}
    for k, v in data.items():
        if k not in known:
            continue
        if isinstance(getattr(cfg, k), tuple) and isinstance(v, list):
            v = tuple(v)
        setattr(cfg, k, v)
        clean[k] = list(v) if isinstance(v, tuple) else v
    p = config_store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return cfg


# ---- diagnostics -------------------------------------------------------


def format_osc(datagram: bytes) -> str:
    """One OSC datagram -> a readable line for the sniffer."""
    msg = osc.decode(datagram)
    if msg is None:
        return f"OSC   ?? undecodable, {len(datagram)} bytes: {datagram[:24].hex()}"
    args = " ".join(repr(a) for a in msg.args)
    return f"OSC   {msg.address}  {args}".rstrip()


def format_midi(raw: bytes) -> str:
    """One MIDI message -> a readable line for the sniffer."""
    ev = mcu.decode(raw)
    if ev is None:
        return f"MIDI  ?? {raw.hex(' ')}"
    return f"MIDI  {ev}"


def sniff(*, recv_port: int = 9000, midi_port: str = "",
          seconds: float = 30.0) -> int:
    """Print everything both sides say, decoded. The first-session tool:

    if MA3's OSC dialect differs from what the bridge expects, it is
    visible here in one fader wiggle, and the config can be adjusted to
    match instead of guessing.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", recv_port))
    except OSError as exc:
        print(f"cannot listen on UDP {recv_port}: {exc} "
              "(stop the bridge while sniffing)", file=sys.stderr)
        return 1
    sock.setblocking(False)

    midi_in = None
    if midi_available():
        import mido
        name = midi_port or find_xtouch_port(mido.get_input_names())
        if name:
            midi_in = mido.open_input(name)
            print(f"listening: MIDI {name!r} + OSC :{recv_port} "
                  f"for {seconds:.0f}s")
        else:
            print(f"no X-Touch found - OSC only on :{recv_port} "
                  f"for {seconds:.0f}s")
    else:
        print(f"mido not installed - OSC only on :{recv_port} "
              f"for {seconds:.0f}s")
    print("wiggle a fader on the surface and one in MA3 now")

    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            quiet = True
            while True:
                try:
                    datagram, addr = sock.recvfrom(4096)
                except BlockingIOError:
                    break
                quiet = False
                print(f"{format_osc(datagram)}    (from {addr[0]})")
            if midi_in is not None:
                for msg in midi_in.iter_pending():
                    quiet = False
                    print(format_midi(bytes(msg.bytes())))
            if quiet:
                time.sleep(0.01)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if midi_in is not None:
            midi_in.close()
    print("done")
    return 0


def selftest(midi_port: str = "") -> int:
    """Wiggle the surface: proves MIDI out and MC mode without MA3."""
    try:
        import mido
    except ImportError:
        print('MIDI support is not installed. Run:  pip install mido python-rtmidi',
              file=sys.stderr)
        return 1

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
        for raw in (mcu.lcd_text(s, 0, "XBridge") for s in range(8)):
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
