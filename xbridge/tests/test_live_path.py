"""The whole path, end to end, with no hardware and no console.

The unit tests prove Bridge.midi_in maps correctly. They do not prove the
thing a user actually runs: Runner.run() opening ports, holding a UDP
socket, and pushing datagrams out of this machine. That gap is exactly
where "the surface connects but the console does nothing" lives, so this
drives the real run loop against a fake X-Touch and a real socket that
stands in for the console.
"""

from __future__ import annotations

import socket
import threading
import time
import types

import pytest

from xbridge import mcu, osc


class FakePort:
    """A MIDI port that hands over bytes we choose, and records sends."""

    def __init__(self, name: str):
        self.name = name
        self.queue: list[bytes] = []
        self.sent: list[bytes] = []
        self.closed = False

    def feed(self, raw: bytes) -> None:
        self.queue.append(raw)

    def iter_pending(self):
        while self.queue:
            yield types.SimpleNamespace(bytes=lambda r=self.queue.pop(0): list(r))

    def send(self, msg) -> None:
        self.sent.append(bytes(msg.data))

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeMido:
    """Just enough mido for the run loop, with Windows-style port names."""

    def __init__(self, ins, outs):
        self._ins, self._outs = ins, outs
        self.opened: list[str] = []
        self.Message = types.SimpleNamespace(
            from_bytes=lambda raw: types.SimpleNamespace(data=raw))

    def get_input_names(self):
        return list(self._ins)

    def get_output_names(self):
        return list(self._outs)

    def open_input(self, name):
        self.opened.append(name)
        return self._ins[name]

    def open_output(self, name):
        self.opened.append(name)
        return self._outs[name]


@pytest.fixture
def console():
    """A UDP socket playing the part of the console's OSC input."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(0.3)
    yield s
    s.close()


def _runner(mido, console, **kw):
    from xbridge import run as xrun

    r = xrun.Runner(ma3_host="127.0.0.1", send_port=console.getsockname()[1],
                    recv_port=0, log=lambda *a: None, **kw)
    r._mido = mido
    return r


def _drive(r, mido, monkeypatch, *, until, timeout=5.0):
    """Run the real loop in a thread until `until()` or timeout."""
    from xbridge import run as xrun

    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    monkeypatch.setattr(xrun, "_RETRY_SECS", 0.05, raising=False)
    t = threading.Thread(target=r.run, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not until():
        time.sleep(0.02)
    r.stop()
    t.join(timeout=3)
    return until()


def _recv_all(sock, *, settle=0.4):
    """Everything the console received, decoded."""
    out = []
    end = time.monotonic() + settle
    while time.monotonic() < end:
        try:
            out.append(osc.decode(sock.recvfrom(4096)[0]))
        except OSError:
            pass
    return [m for m in out if m is not None]


def _xtouch(mido_ins_name="X-Touch 0", out_name="X-Touch 1"):
    ins = {mido_ins_name: FakePort(mido_ins_name)}
    outs = {out_name: FakePort(out_name)}
    return FakeMido(ins, outs), ins[mido_ins_name], outs[out_name]


# ---- the whole path ----------------------------------------------------


def test_a_real_fader_move_reaches_the_console_socket(monkeypatch, console):
    """The end-to-end claim: X-Touch pitch-bend in, OSC out of this
    machine, addressed the way MA3 documents."""
    mido, midi_in, midi_out = _xtouch()
    r = _runner(mido, console)

    def ready():
        return r.state == "running"

    from xbridge import run as xrun
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    t.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and not ready():
            time.sleep(0.02)
        assert ready(), f"never connected: {r.detail}"
        _recv_all(console, settle=0.3)          # drop the hello burst
        midi_in.feed(mcu.fader_out(0, 0.75))    # strip 1 to 75%
        got = _recv_all(console, settle=0.6)
    finally:
        r.stop()
        t.join(timeout=3)

    faders = [m for m in got if "Fader" in m.address]
    assert faders, f"nothing arrived; console saw {got}"
    msg = faders[-1]
    # A stock console: no prefix, int percent - the manual's own example.
    assert msg.address == "/Page1/Fader201"
    assert msg.args[0] == pytest.approx(75, abs=1)
    assert r.counters["midi_in"] >= 1
    assert r.counters["sent"] >= 1


def test_the_surface_is_lit_before_any_fader_moves(monkeypatch, console):
    """If the hello burst never goes out, the strips stay blank and a
    working bridge looks dead."""
    mido, midi_in, midi_out = _xtouch()
    r = _runner(mido, console)
    from xbridge import run as xrun
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    t.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and not midi_out.sent:
            time.sleep(0.02)
    finally:
        r.stop()
        t.join(timeout=3)
    blob = b"".join(midi_out.sent)
    assert b"Ex 201" in blob, "scribble strips were never written"


def test_windows_names_the_two_directions_differently(monkeypatch, console):
    """The field bug: the output was opened with the input's name, and
    Windows calls them 'X-Touch 0' and 'X-Touch 1'."""
    mido, midi_in, midi_out = _xtouch("X-Touch 0", "X-Touch 1")
    r = _runner(mido, console)
    from xbridge import run as xrun
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    t.start()
    try:
        end = time.monotonic() + 5
        while time.monotonic() < end and r.state != "running":
            time.sleep(0.02)
        state = r.state
    finally:
        r.stop()
        t.join(timeout=3)
    assert state == "running", "the pair never opened"
    assert "X-Touch 0" in mido.opened and "X-Touch 1" in mido.opened


def test_console_feedback_drives_the_motor_fader(monkeypatch, console):
    """The return leg: MA3's output has to move the physical fader."""
    from xbridge import run as xrun

    mido, midi_in, midi_out = _xtouch()
    listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listen.bind(("127.0.0.1", 0))
    port = listen.getsockname()[1]
    listen.close()                    # hand the port to the Runner

    r = xrun.Runner(ma3_host="127.0.0.1", send_port=console.getsockname()[1],
                    recv_port=port, log=lambda *a: None)
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    try:
        t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and r.state != "running":
            time.sleep(0.02)
        assert r.state == "running", r.detail
        midi_out.sent.clear()
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.sendto(osc.encode(osc.Message("/Page1/Fader201", (50.0,))),
                   ("127.0.0.1", port))
        out.close()
        end = time.monotonic() + 3
        moves = []
        while time.monotonic() < end and not moves:
            moves = [mcu.decode(raw) for raw in list(midi_out.sent)]
            moves = [m for m in moves if isinstance(m, mcu.FaderMoved)]
            time.sleep(0.02)
    finally:
        r.stop()
        t.join(timeout=3)
    assert moves, "the console's feedback never reached the fader motor"
    assert moves[-1].strip == 0
    assert moves[-1].unit == pytest.approx(0.5, abs=0.02)


def test_a_full_fader_sweep_arrives_in_order_and_in_range(monkeypatch, console):
    """A real hand on a fader is hundreds of messages, not one. They must
    stay ordered, stay inside 0-100, and reach both ends."""
    mido, midi_in, midi_out = _xtouch()
    r = _runner(mido, console)
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    try:
        t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and r.state != "running":
            time.sleep(0.02)
        assert r.state == "running", r.detail
        _recv_all(console, settle=0.3)
        for i in range(0, 101, 5):
            midi_in.feed(mcu.fader_out(0, i / 100))
            time.sleep(0.005)
        got = _recv_all(console, settle=0.8)
    finally:
        r.stop()
        t.join(timeout=3)

    vals = [m.args[0] for m in got if m.address == "/Page1/Fader201"]
    assert len(vals) >= 15, f"only {len(vals)} of 21 arrived"
    assert vals == sorted(vals), "values arrived out of order"
    assert 0 <= min(vals) <= 1 and 99 <= max(vals) <= 100
    assert all(0.0 <= v <= 100.0 for v in vals)


def test_no_surface_says_so_instead_of_dying(monkeypatch, console):
    """An empty MIDI list is the single most common user state. It must
    report, keep waiting, and name what it did see."""
    mido = FakeMido({}, {})
    r = _runner(mido, console)
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    try:
        t.start()
        end = time.monotonic() + 4
        while time.monotonic() < end and r.state != "waiting-for-surface":
            time.sleep(0.02)
        state, detail = r.state, r.detail
    finally:
        r.stop()
        t.join(timeout=3)
    assert state == "waiting-for-surface"
    assert "MC mode" in detail and "none" in detail


def test_every_target_puts_something_on_the_wire(monkeypatch, console):
    """A target that maps a fader to nothing is indistinguishable from a
    broken bridge. None of the shipped ones may be silent."""
    from xbridge.bridge import Config
    from xbridge.targets import TARGETS, make_target

    silent = []
    for name in TARGETS:
        cfg = Config(target=name)
        target = make_target(cfg)
        if getattr(target, "transport", "osc") != "osc":
            continue
        out = target.fader(0, 0.5)
        if not out:
            silent.append(name)
            continue
        for m in out:
            if isinstance(m, osc.Message):
                assert osc.decode(osc.encode(m)) is not None, name
                assert m.address.startswith("/"), (name, m.address)
    assert not silent, f"these targets map a fader to nothing: {silent}"
