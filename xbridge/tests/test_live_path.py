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
    assert "NO MIDI inputs at all" in detail


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


# ---- an X32 in DAW-remote Mackie Control mode --------------------------


def _x32(in_name="X32 0", out_name="X32 1"):
    ins = {in_name: FakePort(in_name)}
    outs = {out_name: FakePort(out_name)}
    return FakeMido(ins, outs), ins[in_name], outs[out_name]


def test_an_x32_in_mc_mode_is_found_and_drives_the_console(monkeypatch,
                                                           console):
    """Setup -> Remote -> Mackie Control makes an X32 a control surface.
    It speaks the same MCU the X-Touch does, so the same fader move must
    come out the other side as the same OSC."""
    from xbridge import run as xrun

    mido, midi_in, midi_out = _x32()
    r = xrun.Runner(ma3_host="127.0.0.1", send_port=console.getsockname()[1],
                    recv_port=0, surface="x32mc", log=lambda *a: None)
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    try:
        t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and r.state != "running":
            time.sleep(0.02)
        assert r.state == "running", f"X32 never connected: {r.detail}"
        _recv_all(console, settle=0.3)
        midi_in.feed(mcu.fader_out(2, 0.5))
        got = _recv_all(console, settle=0.6)
    finally:
        r.stop()
        t.join(timeout=3)
    faders = [m for m in got if "Fader" in m.address]
    assert faders, f"nothing arrived; console saw {got}"
    assert faders[-1].address == "/Page1/Fader203"


def test_the_x32_is_not_sent_scribble_strip_sysex(monkeypatch, console):
    """The X32 draws its own channel names and ignores MCU's LCD SysEx.
    Sending it wastes a DIN-MIDI link's bandwidth at 31250 baud."""
    from xbridge import run as xrun

    mido, midi_in, midi_out = _x32()
    r = xrun.Runner(ma3_host="127.0.0.1", send_port=console.getsockname()[1],
                    recv_port=0, surface="x32mc", log=lambda *a: None)
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    try:
        t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and r.state != "running":
            time.sleep(0.02)
        time.sleep(0.3)
    finally:
        r.stop()
        t.join(timeout=3)
    blob = b"".join(midi_out.sent)
    assert blob, "the X32 was sent nothing at all"
    assert b"\xf0\x00\x00\x66\x14\x12" not in blob, "LCD SysEx was sent"


def test_the_monitor_names_every_control_that_arrives(monkeypatch, console):
    """The point of a hardware test rig: wiggle a control, see what it
    is. A control that produces no line never arrived at all - a
    different problem from a control that is mapped wrong."""
    from xbridge import run as xrun

    mido, midi_in, midi_out = _x32()
    r = xrun.Runner(ma3_host="127.0.0.1", send_port=console.getsockname()[1],
                    recv_port=0, surface="x32mc", log=lambda *a: None)
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    try:
        t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and r.state != "running":
            time.sleep(0.02)
        for raw in (mcu.fader_out(0, 0.75),
                    mcu.fader_out(8, 1.0),
                    bytes((0x90, mcu.SELECT[1], 127)),
                    bytes((0x90, mcu.MUTE[0], 0)),
                    bytes((0xB0, 16, 1)),
                    bytes((0x90, 104, 127)),
                    bytes((0xB0, 99, 7))):        # nothing the bridge knows
            midi_in.feed(raw)
        end = time.monotonic() + 3
        while time.monotonic() < end and len(r.last_midi) < 7:
            time.sleep(0.02)
        seen = list(r.last_midi)
    finally:
        r.stop()
        t.join(timeout=3)

    blob = " | ".join(seen)
    assert "fader 1 -> 75%" in blob
    assert "master -> 100%" in blob
    assert "SELECT 2 down" in blob
    assert "MUTE 1 up" in blob
    assert "encoder 1 +1" in blob
    assert "fader 1 touched" in blob
    # the unknown control still shows its bytes, so it can be identified
    assert "(unmapped)" in blob and "b0 63 07" in blob


def test_the_monitor_does_not_grow_without_bound(monkeypatch, console):
    from xbridge import run as xrun

    r = xrun.Runner(log=lambda *a: None)
    surface = r.bridge.surfaces[0]
    for i in range(200):
        r._note_midi(surface, mcu.fader_out(0, i / 200))
    assert len(r.last_midi) == 12


def test_a_broken_surface_decoder_cannot_stop_the_monitor():
    """Diagnostics must never be the thing that takes the bridge down."""
    from xbridge import run as xrun

    class Exploding:
        name = "boom"

        def decode(self, data):
            raise RuntimeError("nope")

    r = xrun.Runner(log=lambda *a: None)
    r._note_midi(Exploding(), b"\x90\x10\x7f")
    assert r.last_midi and "(unmapped)" in r.last_midi[-1]


def test_a_named_midi_port_wins_over_auto_detection(monkeypatch, console):
    """An X32 over DIN MIDI arrives under the interface's name, and over
    RTPMIDI under the session's - neither is guessable, so naming the
    port has to work even when the name matches no known surface."""
    from xbridge import run as xrun

    name = "Steinberg UR22  1"          # nothing an X32 hint would match
    ins = {name: FakePort(name)}
    outs = {name: FakePort(name)}
    mido = FakeMido(ins, outs)

    r = xrun.Runner(ma3_host="127.0.0.1", send_port=console.getsockname()[1],
                    recv_port=0, surface="x32mc", midi_port=name,
                    log=lambda *a: None)
    monkeypatch.setitem(__import__("sys").modules, "mido", mido)
    t = threading.Thread(target=r.run, daemon=True)
    try:
        t.start()
        end = time.monotonic() + 5
        while time.monotonic() < end and r.state != "running":
            time.sleep(0.02)
        assert r.state == "running", f"named port not used: {r.detail}"
        _recv_all(console, settle=0.3)
        ins[name].feed(mcu.fader_out(0, 1.0))
        got = _recv_all(console, settle=0.6)
    finally:
        r.stop()
        t.join(timeout=3)
    assert [m for m in got if "Fader201" in m.address], f"saw {got}"


def test_the_app_passes_the_named_port_through(monkeypatch):
    """The panel's picker has to reach the Runner, or choosing a port
    silently does nothing."""
    from xbridge import app as xapp
    from xbridge import run as xrun

    seen = {}

    class FakeRunner:
        state, detail, midi_name = "running", "", ""
        counters, last_sent, last_midi = {}, [], []

        def __init__(self, **kw):
            seen.update(kw)

        def run(self):
            return 0

        def stop(self):
            pass

    monkeypatch.setattr(xrun, "Runner", FakeRunner)
    monkeypatch.setattr(xrun, "midi_available", lambda: True)
    xapp._thread = None
    try:
        xapp.api_start(surface="x32mc", midi_port="X-USB 1")
    finally:
        if xapp._thread is not None:
            xapp._thread.join(timeout=2)
        xapp._runner, xapp._thread = None, None
    assert seen["midi_port"] == "X-USB 1"
    assert seen["surface"] == "x32mc"


def test_the_waiting_message_names_the_surface_that_was_chosen():
    """Telling someone to check the X-Touch when they selected an X32
    sends them to the wrong device."""
    from xbridge import run as xrun
    from xbridge.bridge import Config
    from xbridge.surfaces import make_surfaces

    x32 = make_surfaces(Config(surface="x32mc"))
    msg = xrun._no_surface_detail(["Microsoft GS Wavetable Synth"], x32)
    assert "x32mc" in msg
    assert "X-Touch" not in msg
    assert "Card MIDI" in msg and "ENABLE" in msg
    assert "Microsoft GS Wavetable Synth" in msg
    assert "pick it in the MIDI port box" in msg


def test_no_midi_ports_at_all_is_reported_as_its_own_fault():
    """An empty port list is not "the surface is not recognised" - it is
    "nothing is reaching Windows", which no console setting can fix."""
    from xbridge import run as xrun
    from xbridge.bridge import Config
    from xbridge.surfaces import make_surfaces

    msg = xrun._no_surface_detail([], make_surfaces(Config(surface="x32mc")))
    assert "NO MIDI inputs at all" in msg
    assert "driver" in msg and "USB cable" in msg


def test_multiple_surfaces_are_both_named():
    from xbridge import run as xrun
    from xbridge.bridge import Config
    from xbridge.surfaces import make_surfaces

    msg = xrun._no_surface_detail(
        [], make_surfaces(Config(surface="xtouch,x32mc")))
    assert "xtouch + x32mc" in msg


def test_the_x32_advice_names_the_right_usb_socket():
    """The X32 has two USB-B sockets. REMOTE is for X32-Edit and carries
    no MIDI, so plugged into that one the PC sees nothing at all - which
    is indistinguishable from a driver problem unless it is said."""
    from xbridge import run as xrun

    advice = xrun._SURFACE_ADVICE["x32mc"]
    assert "X-USB" in advice and "REMOTE" in advice
    assert "driver" in advice


def test_an_x_live_card_is_recognised_and_its_pair_found():
    """The X32 in the field had an X-LIVE card, not X-USB, and the port
    is named after the card: "X-LIVE MIDI In 0". Auto-detect missed it
    entirely, and the two directions are "In"/"Out" rather than the
    trailing-index pair Windows uses for an X-Touch."""
    from xbridge import run as xrun

    ins = ["X-LIVE MIDI In 0"]
    outs = ["X-LIVE MIDI Out 1"]
    found = xrun.find_surface_port(ins, "x32mc")
    assert found == "X-LIVE MIDI In 0"
    assert xrun._matching_port(found, outs) == "X-LIVE MIDI Out 1"


def test_every_x32_card_flavour_is_recognised():
    from xbridge import run as xrun

    for name in ("X-USB MIDI In 0", "X-LIVE MIDI In 0", "X32 0", "M32 1",
                 "XUSB 0", "XLIVE 2"):
        assert xrun.find_surface_port([name], "x32mc") == name, name


def test_an_x_live_port_does_not_steal_the_xtouch():
    """Both surfaces at once must not cross-assign."""
    from xbridge import run as xrun

    names = ["X-LIVE MIDI In 0", "X-Touch 0"]
    assert xrun.find_surface_port(names, "x32mc") == "X-LIVE MIDI In 0"
    assert xrun.find_surface_port(names, "xtouch") == "X-Touch 0"
