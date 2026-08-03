"""The X-Touch <-> grandMA3 bridge, tested with no hardware in the room.

MCU codec, OSC codec, and the mapping in between are all pure functions,
so a fader move can be traced from MIDI bytes to the OSC datagram MA3
receives - and back from MA3's feedback to the motor-fader message.
"""

from __future__ import annotations

import pytest

from xbridge import mcu, osc
from xbridge.bridge import Bridge, Config
from xbridge.run import default_config_json, find_xtouch_port, load_config

def _cfg(**kw) -> Config:
    """Config with the address shape the mapping tests assert.

    These tests are about the mapping, so they pin the address shape
    explicitly rather than inheriting it, and let
    test_shipped_defaults_match_a_stock_console own the defaults.
    """
    kw.setdefault("prefix", "")
    kw.setdefault("ma3_value", "int100")
    return Config(**kw)


def _plain(**kw) -> Bridge:
    return Bridge(config=_cfg(**kw))


# ---- MCU codec ---------------------------------------------------------


def test_fader_roundtrip():
    ev = mcu.decode(mcu.fader_out(3, 0.5))
    assert isinstance(ev, mcu.FaderMoved)
    assert ev.strip == 3
    assert ev.unit == pytest.approx(0.5, abs=1e-3)


def test_master_fader_is_strip_8():
    ev = mcu.decode(mcu.fader_out(8, 1.0))
    assert ev.strip == 8 and ev.value == 16383


def test_buttons_and_touch_decode():
    assert mcu.decode(bytes((0x90, mcu.SELECT[0], 127))) == \
        mcu.ButtonPressed(mcu.SELECT[0], True)
    assert mcu.decode(bytes((0x90, mcu.SELECT[0], 0))) == \
        mcu.ButtonPressed(mcu.SELECT[0], False)
    assert mcu.decode(bytes((0x90, 104, 127))) == mcu.FaderTouched(0, True)
    assert mcu.decode(bytes((0x80, 112, 0))) == mcu.FaderTouched(8, False)


def test_encoder_relative_decode():
    assert mcu.decode(bytes((0xB0, 16, 1))) == mcu.EncoderTurned(0, 1)
    assert mcu.decode(bytes((0xB0, 23, 65))) == mcu.EncoderTurned(7, -1)
    assert mcu.decode(bytes((0xB0, 17, 3))) == mcu.EncoderTurned(1, 3)


def test_lcd_text_is_seven_ascii_chars_at_the_right_offset():
    msg = mcu.lcd_text(2, 1, "Exec 201 too long")
    assert msg[:6] == bytes((0xF0, 0x00, 0x00, 0x66, 0x14, 0x12))
    assert msg[6] == 56 + 2 * 7          # line 1 base + strip offset
    assert msg[7:14] == b"Exec 20"       # truncated to 7
    assert msg[-1] == 0xF7
    # non-ASCII must not produce SysEx-breaking bytes
    weird = mcu.lcd_text(0, 0, "ø∆é")
    assert all(b < 0x80 for b in weird[7:14])


def test_junk_midi_decodes_to_none():
    for junk in (b"", b"\xfe", bytes((0xB0, 99, 1)), bytes((0xC0, 1))):
        assert mcu.decode(junk) is None


# ---- OSC codec ---------------------------------------------------------


def test_osc_roundtrip_int_float_string():
    for msg in (osc.Message("/Page1/Fader201", (75,)),
                osc.Message("/Page1/Key101", (1,)),
                osc.Message("/cmd", ("Master 2.1 At 80",)),
                osc.Message("/x", (0.5,)),
                osc.Message("/bare")):
        back = osc.decode(osc.encode(msg))
        assert back is not None
        assert back.address == msg.address
        for a, b in zip(msg.args, back.args):
            assert a == pytest.approx(b) if isinstance(a, float) else a == b


def test_osc_padding_is_four_byte_aligned():
    data = osc.encode(osc.Message("/abc", ("x",)))
    assert len(data) % 4 == 0


def test_osc_garbage_never_raises():
    for junk in (b"", b"\x00\x00", b"no slash\x00", b"/a\x00\x00,i\x00\x00"):
        osc.decode(junk)     # must not raise; value may be None or partial


# ---- the bridge, surface -> MA3 ---------------------------------------


def test_fader_move_becomes_ma3_executor_level():
    b = _plain()
    (datagram,) = b.midi_in(mcu.fader_out(0, 0.75))
    msg = osc.decode(datagram)
    assert msg.address == "/Page1/Fader201"
    assert msg.args == (75,)


def test_prefix_and_page_shape_the_address():
    b = Bridge(config=Config(prefix="gMA3", page=3))
    (datagram,) = b.midi_in(mcu.fader_out(1, 1.0))
    assert osc.decode(datagram).address == "/gMA3/Page3/Fader202"


def test_master_fader_uses_the_command_line():
    b = _plain()
    (datagram,) = b.midi_in(mcu.fader_out(8, 0.8))
    msg = osc.decode(datagram)
    assert msg.address == "/cmd"
    assert "Master 2.1 At 80" in msg.args[0]


def test_select_button_press_and_release_hit_the_key():
    b = _plain()
    (down,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 127)))
    (up,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 0)))
    assert osc.decode(down) == osc.Message("/Page1/Key101", (1,))
    assert osc.decode(up) == osc.Message("/Page1/Key101", (0,))


def test_encoder_ticks_accumulate_and_clamp():
    b = _plain()
    for _ in range(3):
        (d,) = b.midi_in(bytes((0xB0, 16, 1)))
    assert osc.decode(d).args == (6,)          # 3 ticks * 2% = 6
    for _ in range(100):
        (d,) = b.midi_in(bytes((0xB0, 16, 65)))
    assert osc.decode(d).args == (0,)          # clamped at the bottom


def test_bank_buttons_flip_the_page():
    b = _plain()
    b.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))
    assert b.config.page == 2
    (datagram,) = b.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(datagram).address == "/Page2/Fader201"
    b.midi_in(bytes((0x90, mcu.FADER_BANK_LEFT, 127)))
    b.midi_in(bytes((0x90, mcu.FADER_BANK_LEFT, 127)))
    assert b.config.page == 1                  # never below page 1


# ---- the bridge, MA3 -> surface ---------------------------------------


def test_ma3_fader_feedback_moves_the_motor():
    b = _plain()
    (raw,) = b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (50,))))
    ev = mcu.decode(raw)
    assert isinstance(ev, mcu.FaderMoved)
    assert ev.strip == 0
    assert ev.unit == pytest.approx(0.5, abs=0.01)


def test_feedback_never_fights_a_touched_fader():
    b = _plain()
    b.midi_in(bytes((0x90, 104, 127)))         # finger down on strip 1
    assert b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (99,)))) == []
    b.midi_in(bytes((0x90, 104, 0)))           # finger off
    assert b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (99,)))) != []


def test_feedback_for_another_page_is_ignored():
    b = _plain()
    assert b.osc_in(osc.encode(osc.Message("/Page9/Fader201", (50,)))) == []


def test_key_feedback_lights_the_button_led():
    b = _plain()
    (raw,) = b.osc_in(osc.encode(osc.Message("/Page1/Key101", (1,))))
    assert raw == mcu.button_led(mcu.SELECT[0], True)


def test_encoder_feedback_paints_the_ring():
    b = _plain()
    (raw,) = b.osc_in(osc.encode(osc.Message("/Page1/Fader301", (100,))))
    assert raw[0] == 0xB0 and raw[1] == 48     # ring CC for encoder 1


def test_float_and_junk_feedback_are_safe():
    b = _plain()
    (raw,) = b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (50.0,))))
    assert mcu.decode(raw).unit == pytest.approx(0.5, abs=0.01)
    assert b.osc_in(b"\x01\x02not osc") == []
    assert b.osc_in(osc.encode(osc.Message("/Page1/FaderNaN", (1,)))) == []


def test_hello_labels_the_strips():
    payloads = b"".join(_plain().hello())
    assert b"Ex 201" in payloads
    assert b"Pg 1" in payloads


# ---- config ------------------------------------------------------------


def test_config_json_roundtrip(tmp_path):
    p = tmp_path / "map.json"
    p.write_text(default_config_json(), encoding="utf-8")
    cfg = load_config(p)
    assert cfg == Config()


def test_config_overrides_and_unknown_keys(tmp_path, capsys):
    p = tmp_path / "map.json"
    p.write_text('{"page": 4, "fader_execs": [401, 402], "wat": 1}',
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg.page == 4
    assert cfg.fader_execs == (401, 402)
    assert "wat" in capsys.readouterr().err


def test_port_finder_matches_xtouch_names():
    assert find_xtouch_port(["Foo", "X-Touch INT 0"]) == "X-Touch INT 0"
    assert find_xtouch_port(["XTOUCH 1"]) == "XTOUCH 1"
    assert find_xtouch_port(["LoopBe", "IAC"]) is None


# ---- transport row -----------------------------------------------------


def test_transport_buttons_fire_commands_on_press_only():
    b = _plain()
    (down,) = b.midi_in(bytes((0x90, mcu.PLAY, 127)))
    assert osc.decode(down) == osc.Message("/cmd", ("Go+",))
    assert b.midi_in(bytes((0x90, mcu.PLAY, 0))) == []       # release: nothing
    (stop,) = b.midi_in(bytes((0x90, mcu.STOP, 127)))
    assert osc.decode(stop).args == ("Pause",)
    (rew,) = b.midi_in(bytes((0x90, mcu.REWIND, 127)))
    assert osc.decode(rew).args == ("Go-",)


def test_unmapped_transport_button_stays_silent():
    b = _plain()
    assert b.midi_in(bytes((0x90, mcu.RECORD, 127))) == []
    b.config.cmd_record = "Off Sequence 1"
    (d,) = b.midi_in(bytes((0x90, mcu.RECORD, 127)))
    assert osc.decode(d).args == ("Off Sequence 1",)


# ---- runner and diagnostics -------------------------------------------


def test_runner_errors_cleanly_when_the_udp_port_is_taken():
    """The conflict socket must bind the wildcard address: on Windows a
    0.0.0.0 bind SUCCEEDS alongside an existing 127.0.0.1 bind, so a
    loopback decoy doesn't conflict there - which turned this test into an
    infinite wait-for-surface loop on the Windows CI runner."""
    import socket
    import threading

    from xbridge.run import Runner, midi_available

    if not midi_available():
        pytest.skip("mido not installed here; the port check needs run()")
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("0.0.0.0", 0))
    port = taken.getsockname()[1]
    try:
        r = Runner(recv_port=port, log=lambda *a: None)
        result: list[int] = []
        t = threading.Thread(target=lambda: result.append(r.run()))
        t.start()
        t.join(timeout=10)
        if t.is_alive():          # belt and braces: never hang the suite
            r.stop()
            t.join(timeout=5)
            pytest.fail("Runner.run() did not return on a taken port")
        assert result == [1]
        assert r.state == "error"
        assert "cannot listen" in r.detail
    finally:
        taken.close()


def test_runner_stop_event_ends_the_wait_for_a_surface():
    from xbridge.run import Runner, midi_available

    if not midi_available():
        pytest.skip("mido not installed here")
    r = Runner(recv_port=0, midi_port="", log=lambda *a: None)
    r.stop()                      # stopped before it starts: run returns fast
    assert r.run() == 0
    assert r.state == "stopped"


def test_sniffer_formatting_is_readable_and_junk_proof():
    from xbridge.run import format_midi, format_osc

    line = format_osc(osc.encode(osc.Message("/Page1/Fader201", (75,))))
    assert "/Page1/Fader201" in line and "75" in line
    assert "undecodable" in format_osc(b"\x01\x02\x03")
    assert "FaderMoved" in format_midi(mcu.fader_out(0, 1.0))
    assert "??" in format_midi(b"\xfe")


def test_web_status_endpoint_reports_without_midi_installed():
    from xbridge import app as web

    d = web.api_status()
    assert set(d) >= {"available", "running", "state", "detail"}
    assert d["running"] is False


# ---- the X32 audio target ---------------------------------------------


def _x32() -> Bridge:
    return Bridge(config=_cfg(target="x32"))


def test_x32_fader_is_a_channel_level():
    (d,) = _x32().midi_in(mcu.fader_out(0, 0.5))
    msg = osc.decode(d)
    assert msg.address == "/ch/01/mix/fader"
    assert msg.args[0] == pytest.approx(0.5, abs=0.001)


def test_x32_banks_are_pages_of_eight_channels():
    b = _x32()
    b.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))     # bank 2
    (d,) = b.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(d).address == "/ch/09/mix/fader"
    for _ in range(10):                                     # clamps at bank 4
        b.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))
    assert b.config.page == 4
    (d,) = b.midi_in(mcu.fader_out(7, 1.0))
    assert osc.decode(d).address == "/ch/32/mix/fader"


def test_x32_mute_is_a_toggle_that_tracks_console_state():
    b = _x32()
    (d,) = b.midi_in(bytes((0x90, mcu.MUTE[0], 127)))        # press: mute
    assert osc.decode(d) == osc.Message("/ch/01/mix/on", (0,))
    assert b.midi_in(bytes((0x90, mcu.MUTE[0], 0))) == []    # release: nothing
    (d,) = b.midi_in(bytes((0x90, mcu.MUTE[0], 127)))        # press: unmute
    assert osc.decode(d) == osc.Message("/ch/01/mix/on", (1,))
    # Console reports someone else muted ch1: next press must unmute.
    b.osc_in(osc.encode(osc.Message("/ch/01/mix/on", (0,))))
    (d,) = b.midi_in(bytes((0x90, mcu.MUTE[0], 127)))
    assert osc.decode(d) == osc.Message("/ch/01/mix/on", (1,))


def test_x32_select_and_encoder_and_master():
    b = _x32()
    (sel,) = b.midi_in(bytes((0x90, mcu.SELECT[2], 127)))
    assert osc.decode(sel) == osc.Message("/-stat/selidx", (2,))
    (pan,) = b.midi_in(bytes((0xB0, 16, 1)))
    assert osc.decode(pan).address == "/ch/01/mix/pan"
    (mn,) = b.midi_in(mcu.fader_out(8, 1.0))
    assert osc.decode(mn).address == "/main/st/mix/fader"


def test_x32_hello_subscribes_and_queries_names():
    dgrams = [osc.decode(d) for d in _x32().osc_hello()]
    addrs = [m.address for m in dgrams]
    assert "/xremote" in addrs
    assert "/ch/01/config/name" in addrs
    assert "/main/st/mix/fader" in addrs
    # queries carry no arguments - that's what makes them queries
    assert all(not m.args for m in dgrams)


def test_x32_tick_renews_the_subscription_but_not_constantly():
    b = _x32()
    first = b.tick(100.0)
    assert [osc.decode(d).address for d in first] == ["/xremote"]
    assert b.tick(101.0) == []                 # too soon
    assert b.tick(109.0) != []                 # 8s later: renew


def test_x32_feedback_names_reach_the_scribble_strips():
    b = _x32()
    (raw,) = b.osc_in(osc.encode(osc.Message("/ch/01/config/name", ("Kick",))))
    assert raw == mcu.lcd_text(0, 0, "Kick")
    # a name outside the visible bank is remembered, not displayed
    assert b.osc_in(osc.encode(
        osc.Message("/ch/12/config/name", ("Vox",)))) == []


def test_x32_feedback_fader_and_mute_reach_the_surface():
    b = _x32()
    (motor,) = b.osc_in(osc.encode(osc.Message("/ch/03/mix/fader", (0.75,))))
    ev = mcu.decode(motor)
    assert ev.strip == 2 and ev.unit == pytest.approx(0.75, abs=0.01)
    led, label = b.osc_in(osc.encode(osc.Message("/ch/01/mix/on", (0,))))
    assert led == mcu.button_led(mcu.MUTE[0], True)
    assert label == mcu.lcd_text(0, 1, "MUTED")


def test_unknown_target_is_rejected():
    from xbridge import targets

    with pytest.raises(ValueError):
        targets.make_target(Config(target="hog4"))


# ---- MagicQ ------------------------------------------------------------


def _magicq() -> Bridge:
    return Bridge(config=_cfg(target="magicq"))


def test_magicq_faders_ride_playbacks():
    (d,) = _magicq().midi_in(mcu.fader_out(0, 0.5))
    msg = osc.decode(d)
    assert msg.address == "/pb/1"
    assert msg.args[0] == pytest.approx(0.5, abs=0.001)
    (m,) = _magicq().midi_in(mcu.fader_out(8, 1.0))
    assert osc.decode(m).address == "/pb/9"        # master -> playback 9


def test_magicq_go_and_true_flash():
    b = _magicq()
    (go,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 127)))
    assert osc.decode(go) == osc.Message("/pb/1/go", (1,))
    (fl_dn,) = b.midi_in(bytes((0x90, mcu.MUTE[3], 127)))
    (fl_up,) = b.midi_in(bytes((0x90, mcu.MUTE[3], 0)))
    assert osc.decode(fl_dn) == osc.Message("/pb/4/flash", (1,))
    assert osc.decode(fl_up) == osc.Message("/pb/4/flash", (0,))


def test_magicq_stop_is_blackout_play_restores():
    b = _magicq()
    (dbo,) = b.midi_in(bytes((0x90, mcu.STOP, 127)))
    assert osc.decode(dbo) == osc.Message("/dbo", (1,))
    (un,) = b.midi_in(bytes((0x90, mcu.PLAY, 127)))
    assert osc.decode(un) == osc.Message("/dbo", (0,))


def test_magicq_hello_subscribes_feedback_and_pb_feedback_moves_motors():
    b = _magicq()
    assert [osc.decode(d).address for d in b.osc_hello()] == ["/feedback/pb+exec"]
    (motor,) = b.osc_in(osc.encode(osc.Message("/pb/2", (0.6,))))
    ev = mcu.decode(motor)
    assert ev.strip == 1 and ev.unit == pytest.approx(0.6, abs=0.01)
    (ring,) = b.osc_in(osc.encode(osc.Message("/exec/1/3", (0.5,))))
    assert ring[1] == 48 + 2                       # ring CC for encoder 3


def test_magicq_has_no_paging():
    b = _magicq()
    b.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))
    assert b.config.page == 1


# ---- Resolume ----------------------------------------------------------


def _resolume() -> Bridge:
    return Bridge(config=_cfg(target="resolume"))


def test_resolume_faders_are_layer_opacity_banked():
    b = _resolume()
    (d,) = b.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(d).address == "/composition/layers/1/video/opacity"
    b.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))
    (d,) = b.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(d).address == "/composition/layers/9/video/opacity"
    (m,) = b.midi_in(mcu.fader_out(8, 0.5))
    assert osc.decode(m).address == "/composition/master"


def test_resolume_column_connect_and_layer_bypass_toggle():
    b = _resolume()
    (col,) = b.midi_in(bytes((0x90, mcu.SELECT[1], 127)))
    assert osc.decode(col) == osc.Message("/composition/columns/2/connect", (1,))
    (byp,) = b.midi_in(bytes((0x90, mcu.MUTE[0], 127)))
    assert osc.decode(byp) == osc.Message("/composition/layers/1/bypassed", (1,))
    (unbyp,) = b.midi_in(bytes((0x90, mcu.MUTE[0], 127)))
    assert osc.decode(unbyp).args == (0,)


def test_resolume_feedback_drives_motors_and_leds():
    b = _resolume()
    (motor,) = b.osc_in(osc.encode(
        osc.Message("/composition/layers/2/video/opacity", (0.3,))))
    assert mcu.decode(motor).strip == 1
    (led,) = b.osc_in(osc.encode(
        osc.Message("/composition/layers/1/bypassed", (1,))))
    assert led == mcu.button_led(mcu.MUTE[0], True)


# ---- Companion ---------------------------------------------------------


def _companion() -> Bridge:
    return Bridge(config=_cfg(target="companion"))


def test_companion_buttons_are_locations_with_true_down_up():
    b = _companion()
    (dn,) = b.midi_in(bytes((0x90, mcu.SELECT[2], 127)))
    (up,) = b.midi_in(bytes((0x90, mcu.SELECT[2], 0)))
    assert osc.decode(dn).address == "/location/1/0/2/down"
    assert osc.decode(up).address == "/location/1/0/2/up"
    (mt,) = b.midi_in(bytes((0x90, mcu.MUTE[5], 127)))
    assert osc.decode(mt).address == "/location/1/1/5/down"


def test_companion_transport_hits_row_two():
    b = _companion()
    (play,) = b.midi_in(bytes((0x90, mcu.PLAY, 127)))
    assert osc.decode(play).address == "/location/1/2/3/down"
    (rew,) = b.midi_in(bytes((0x90, mcu.REWIND, 127)))
    assert osc.decode(rew).address == "/location/1/2/0/down"


def test_companion_faders_write_custom_variables():
    (d,) = _companion().midi_in(mcu.fader_out(0, 0.5))
    msg = osc.decode(d)
    assert msg.address == "/custom-variable/fader1/value"
    assert msg.args == (50,)
    (m,) = _companion().midi_in(mcu.fader_out(8, 1.0))
    assert osc.decode(m).address == "/custom-variable/master/value"


def test_companion_encoders_send_rotate_events():
    b = _companion()
    dgrams = b.midi_in(bytes((0xB0, 16, 3)))       # 3 clockwise ticks
    addrs = [osc.decode(d).address for d in dgrams]
    assert addrs == ["/location/1/3/0/rotate-right"] * 3
    (left,) = b.midi_in(bytes((0xB0, 17, 65)))
    assert osc.decode(left).address == "/location/1/3/1/rotate-left"


def test_companion_bank_changes_the_companion_page():
    b = _companion()
    b.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))
    (dn,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 127)))
    assert osc.decode(dn).address == "/location/2/0/0/down"


# ---- ETC Eos -----------------------------------------------------------


def _eos() -> Bridge:
    return Bridge(config=_cfg(target="eos"))


def test_eos_hello_configures_the_fader_bank():
    (d,) = _eos().osc_hello()
    msg = osc.decode(d)
    assert msg.address == "/eos/fader/1/config/10"
    assert msg.args == ()


def test_eos_faders_are_bank_floats():
    (d,) = _eos().midi_in(mcu.fader_out(2, 0.5))
    msg = osc.decode(d)
    assert msg.address == "/eos/fader/1/3"
    assert msg.args[0] == pytest.approx(0.5, abs=0.001)
    assert _eos().midi_in(mcu.fader_out(8, 1.0)) == []   # no OSC grand master


def test_eos_fire_stop_and_master_keys():
    b = _eos()
    (dn,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 127)))
    (up,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 0)))
    assert osc.decode(dn) == osc.Message("/eos/fader/1/1/fire", (1.0,))
    assert osc.decode(up) == osc.Message("/eos/fader/1/1/fire", (0.0,))
    (go,) = b.midi_in(bytes((0x90, mcu.PLAY, 127)))
    assert osc.decode(go).address == "/eos/key/go_0"
    (stop,) = b.midi_in(bytes((0x90, mcu.STOP, 127)))
    assert osc.decode(stop).address == "/eos/key/stop"


def test_eos_feedback_moves_the_motors():
    b = _eos()
    (motor,) = b.osc_in(osc.encode(osc.Message("/eos/out/fader/1/2", (0.4,))))
    ev = mcu.decode(motor)
    assert ev.strip == 1 and ev.unit == pytest.approx(0.4, abs=0.01)
    assert b.osc_in(osc.encode(osc.Message("/eos/out/fader/2/1", (0.4,)))) == []


# ---- generic OSC template target --------------------------------------


def test_generic_templates_fill_the_strip_number():
    b = Bridge(config=_cfg(target="generic", gen_fader="/qlab/cue/{n}/level",
                             gen_scale="float01"))
    (d,) = b.midi_in(mcu.fader_out(0, 0.5))
    msg = osc.decode(d)
    assert msg.address == "/qlab/cue/1/level"
    assert msg.args[0] == pytest.approx(0.5, abs=0.001)
    b.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))     # page 2
    (d,) = b.midi_in(mcu.fader_out(0, 0.5))
    assert osc.decode(d).address == "/qlab/cue/9/level"


def test_generic_int_scale_and_buttons_and_unmapped():
    b = Bridge(config=_cfg(target="generic", gen_scale="int100",
                             gen_mute=""))
    (d,) = b.midi_in(mcu.fader_out(1, 0.5))
    assert osc.decode(d).args == (50,)
    (dn,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 127)))
    assert osc.decode(dn) == osc.Message("/button/1", (1,))
    assert b.midi_in(bytes((0x90, mcu.MUTE[0], 127))) == []  # unmapped row


def test_generic_feedback_on_the_fader_template_moves_motors():
    b = Bridge(config=_cfg(target="generic", gen_fader="/fader/{n}"))
    (motor,) = b.osc_in(osc.encode(osc.Message("/fader/3", (0.25,))))
    assert mcu.decode(motor).strip == 2
    assert b.osc_in(osc.encode(osc.Message("/other/3", (0.25,)))) == []


# ---- MA3 label plugin feedback ----------------------------------------


def test_ma3_lua_plugin_labels_reach_the_strips():
    b = _plain()
    (raw,) = b.osc_in(osc.encode(
        osc.Message("/xbridge/label/1", ("Front Wash",))))
    assert raw == mcu.lcd_text(0, 0, "Front Wash")
    assert b.osc_in(osc.encode(osc.Message("/xbridge/label/9", ("x",)))) == []


def test_ma3_plugin_file_ships_and_names_the_contract():
    from pathlib import Path

    lua = Path(__file__).parent.parent / "ma3-plugin" / "xbridge_labels.lua"
    text = lua.read_text(encoding="utf-8")
    assert "/xbridge/label/" in text
    assert "SendOSC" in text


# ---- presets and OCR endpoints ----------------------------------------


def test_preset_export_import_roundtrip(tmp_path, monkeypatch):
    import asyncio
    import json as jsonlib
    from pathlib import Path

    from xbridge import app as web

    monkeypatch.setenv("XBRIDGE_CONFIG", str(tmp_path / "xtouch.json"))
    resp = web.api_config_export()
    exported = jsonlib.loads(Path(resp.path).read_text(encoding="utf-8"))
    assert exported["target"] == "ma3"

    class FakeUpload:
        filename = "preset.json"

        async def read(self):
            return jsonlib.dumps({"target": "generic",
                                  "gen_fader": "/x/{n}"}).encode()

    d = asyncio.run(web.api_config_import(FakeUpload()))
    assert d["target"] == "generic"
    from xbridge.run import load_stored_config
    assert load_stored_config().gen_fader == "/x/{n}"


def test_stored_mapping_roundtrip(tmp_path, monkeypatch):
    from xbridge.run import load_stored_config, store_config

    monkeypatch.setenv("XBRIDGE_CONFIG", str(tmp_path / "xtouch.json"))
    assert load_stored_config() == Config()          # nothing stored yet
    store_config({"target": "x32", "page": 3, "fader_execs": [401, 402],
                  "wat": "dropped"})
    cfg = load_stored_config()
    assert cfg.target == "x32"
    assert cfg.page == 3
    assert cfg.fader_execs == (401, 402)


def test_web_config_endpoints_roundtrip(tmp_path, monkeypatch):
    import asyncio
    import json as jsonlib

    from xbridge import app as web

    monkeypatch.setenv("XBRIDGE_CONFIG", str(tmp_path / "xtouch.json"))
    d = web.api_config()
    assert d["config"]["target"] == "ma3"
    assert d["config"]["fader_execs"] == list(range(201, 209))

    class FakeRequest:
        async def json(self):
            return {"target": "x32", "page": 2}

    asyncio.run(web.api_config_save(FakeRequest()))
    assert web.api_config()["config"]["target"] == "x32"
    assert jsonlib.loads((tmp_path / "xtouch.json").read_text())["page"] == 2


# ---- grandMA2 (web remote) --------------------------------------------


def _ma2() -> Bridge:
    return Bridge(config=_cfg(target="ma2"))


def test_ma2_faders_are_executor_commands():
    b = _ma2()
    (cmd,) = b.midi_in(mcu.fader_out(0, 0.5))
    assert isinstance(cmd, str)
    assert cmd == "Executor 1.1 At 50.0"
    b.config.page = 3
    (cmd,) = b.midi_in(mcu.fader_out(7, 1.0))
    assert cmd == "Executor 3.8 At 100.0"


def test_ma2_master_and_buttons_and_encoders():
    b = _ma2()
    (m,) = b.midi_in(mcu.fader_out(8, 0.8))
    assert m == "SpecialMaster 2.1 At 80.0"
    (go,) = b.midi_in(bytes((0x90, mcu.SELECT[1], 127)))
    assert go == "Go Executor 1.2"
    assert b.midi_in(bytes((0x90, mcu.SELECT[1], 0))) == []
    (off,) = b.midi_in(bytes((0x90, mcu.MUTE[0], 127)))
    assert off == "Off Executor 1.1"
    (enc,) = b.midi_in(bytes((0xB0, 16, 1)))
    assert enc.startswith("Executor 1.9 At ")


def test_ma2_master_command_is_a_template():
    b = Bridge(config=_cfg(target="ma2", ma2_master_cmd="Master 2.2 At {pct}"))
    (m,) = b.midi_in(mcu.fader_out(8, 0.5))
    assert m == "Master 2.2 At 50.0"
    b2 = Bridge(config=_cfg(target="ma2", ma2_master_cmd=""))
    assert b2.midi_in(mcu.fader_out(8, 0.5)) == []


def test_ma2_playbacks_request_names_the_session():
    t = _ma2().target
    req = t.playbacks_request(42)
    assert req["requestType"] == "playbacks"
    assert req["session"] == 42


def test_ma2_ws_feedback_drives_motors_and_labels():
    b = _ma2()
    msg = {
        "responseType": "playbacks",
        "itemGroups": [{"items": [[
            {"iExec": 0, "i": {"t": "Front Wash"},
             "executorBlocks": [{"fader": {"v": 0.5}}]},
            {"iExec": 1, "i": {"t": ""},
             "executorBlocks": [{"fader": {"v": 1.0}}]},
        ]]}],
    }
    out = b.apply_feedback(b.target.ws_feedback(msg))
    assert mcu.lcd_text(0, 0, "Front Wash") in out
    motors = [mcu.decode(r) for r in out if mcu.decode(r) is not None
              and isinstance(mcu.decode(r), mcu.FaderMoved)]
    assert any(m.strip == 0 and abs(m.unit - 0.5) < 0.01 for m in motors)
    assert any(m.strip == 1 and m.unit > 0.99 for m in motors)
    # a repeat poll must not spam the label again (levels may repeat)
    from xbridge import targets
    again = b.target.ws_feedback(msg)
    assert not any(isinstance(fb, targets.LabelFB) for fb in again)


def test_ma2_ws_feedback_is_garbage_proof():
    t = _ma2().target
    assert t.ws_feedback({}) == []
    assert t.ws_feedback({"responseType": "playbacks", "itemGroups": "?"}) == []
    assert t.ws_feedback({"responseType": "playbacks",
                          "itemGroups": [{"items": [[{"iExec": 99}]]}]}) == []


def test_ma2_touch_suppression_applies_to_ws_feedback_too():
    b = _ma2()
    b.midi_in(bytes((0x90, 104, 127)))     # finger on strip 1
    msg = {"responseType": "playbacks", "itemGroups": [{"items": [[
        {"iExec": 0, "executorBlocks": [{"fader": {"v": 0.9}}]}]]}]}
    out = b.apply_feedback(b.target.ws_feedback(msg))
    assert all(not isinstance(mcu.decode(r), mcu.FaderMoved) for r in out)


# ---- surfaces: MPK Mini and the OSC control port ----------------------


def _mpk() -> Bridge:
    return Bridge(config=_cfg(surface="mpk"))


def test_mpk_knobs_ride_encoder_slots_absolutely():
    b = _mpk()
    (d,) = b.midi_in(bytes((0xB0, 70, 64)))          # knob 1 at half
    msg = osc.decode(d)
    assert msg.address == "/Page1/Fader301"
    assert msg.args == (50,)
    (d,) = b.midi_in(bytes((0xB0, 77, 127)))         # knob 8 full
    assert osc.decode(d).address == "/Page1/Fader308"


def test_mpk_pads_press_the_select_row_with_led_feedback():
    b = _mpk()
    (dn,) = b.midi_in(bytes((0x90, 36, 100)))
    assert osc.decode(dn) == osc.Message("/Page1/Key101", (1,))
    (up,) = b.midi_in(bytes((0x80, 36, 0)))
    assert osc.decode(up) == osc.Message("/Page1/Key101", (0,))
    # console reports the exec on: the pad lights
    (led,) = b.osc_in(osc.encode(osc.Message("/Page1/Key101", (1,))))
    assert led == bytes((0x90, 36, 127))
    # motors/labels have nowhere to go on an MPK: silently dropped
    assert b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (50,)))) == []


def test_mpk_ignores_unmapped_midi_and_custom_numbers_work():
    b = _mpk()
    assert b.midi_in(bytes((0xB0, 1, 64))) == []     # mod wheel: not a knob
    b2 = Bridge(config=_cfg(surface="mpk", mpk_knob_ccs=(20, 21),
                              mpk_pad_notes=(40,)))
    (d,) = b2.midi_in(bytes((0xB0, 21, 127)))
    assert osc.decode(d).address == "/Page1/Fader302"


def test_mpk_hello_clears_the_pads():
    hello = _mpk().hello()
    assert bytes((0x90, 36, 0)) in hello
    assert len(hello) == 8                            # pads only - no motors


def test_unknown_surface_is_rejected():
    from xbridge import surfaces

    with pytest.raises(ValueError):
        surfaces.make_surface(Config(surface="pushctl"))


def test_control_port_reaches_every_path():
    b = _plain()
    (d,) = b.control_in(osc.Message("/xbridge/fader/1", (0.5,)))
    assert osc.decode(d) == osc.Message("/Page1/Fader201", (50,))
    (d,) = b.control_in(osc.Message("/xbridge/fader/2", (75,)))   # int 0-100
    assert osc.decode(d).args == (75,)
    (d,) = b.control_in(osc.Message("/xbridge/key/select/3", (1,)))
    assert osc.decode(d) == osc.Message("/Page1/Key103", (1,))
    (d,) = b.control_in(osc.Message("/xbridge/key/mute/1", (0,)))
    assert osc.decode(d) == osc.Message("/Page1/Key291", (0,))
    (d,) = b.control_in(osc.Message("/xbridge/enc/1", (1.0,)))
    assert osc.decode(d).address == "/Page1/Fader301"
    b.control_in(osc.Message("/xbridge/page", (3,)))
    assert b.config.page == 3
    (d,) = b.control_in(osc.Message("/xbridge/fader/1", (1.0,)))
    assert osc.decode(d).address == "/Page3/Fader201"


def test_control_port_rejects_junk_quietly():
    b = _plain()
    for addr in ("/xbridge/fader/99", "/xbridge/fader/x", "/xbridge/nope",
                 "/xbridge/key/rec/1", "/other/fader/1"):
        assert b.control_in(osc.Message(addr, (1,))) == []


# ---- multiple surfaces at once ----------------------------------------


def test_surface_list_parses_and_rejects():
    from xbridge import surfaces

    both = surfaces.make_surfaces(Config(surface="xtouch,mpk"))
    assert [s.name for s in both] == ["xtouch", "mpk"]
    plus = surfaces.make_surfaces(Config(surface="xtouch + mpk"))
    assert [s.name for s in plus] == ["xtouch", "mpk"]
    with pytest.raises(ValueError):
        surfaces.make_surfaces(Config(surface="xtouch,decks"))


def test_events_route_by_originating_surface():
    b = Bridge(config=_cfg(surface="xtouch,mpk"))
    xt, mpk = b.surfaces
    # CC 70 is a knob on the MPK but nothing on the X-Touch
    assert b.midi_in(bytes((0xB0, 70, 127)), surface=xt) == []
    (d,) = b.midi_in(bytes((0xB0, 70, 127)), surface=mpk)
    assert osc.decode(d).address == "/Page1/Fader301"
    # note 36 is REC row on MCU (unmapped) but pad 1 on the MPK
    assert b.midi_in(bytes((0x90, 36, 127)), surface=xt) == []
    (d,) = b.midi_in(bytes((0x90, 36, 127)), surface=mpk)
    assert osc.decode(d) == osc.Message("/Page1/Key101", (1,))


def test_feedback_renders_per_surface():
    b = Bridge(config=_cfg(surface="xtouch,mpk"))
    xt, mpk = b.surfaces
    intents = b.target.feedback(osc.Message("/Page1/Key101", (1,)))
    assert b.render_for(xt, intents) == [mcu.button_led(mcu.SELECT[0], True)]
    assert b.render_for(mpk, intents) == [bytes((0x90, 36, 127))]
    fader = b.target.feedback(osc.Message("/Page1/Fader201", (50,)))
    assert b.render_for(mpk, fader) == []          # no motors on an MPK
    assert len(b.render_for(xt, fader)) == 1


def test_touch_suppression_holds_across_surfaces():
    b = Bridge(config=_cfg(surface="xtouch,mpk"))
    xt, _mpk = b.surfaces
    b.midi_in(bytes((0x90, 104, 127)), surface=xt)   # finger on fader 1
    fader = b.target.feedback(osc.Message("/Page1/Fader201", (99,)))
    assert b.render_for(xt, fader) == []


# ---- MIDI port pairing (the Windows "unknown port" bug) ---------------


class _Handle:
    def __init__(self, name, log):
        self.name, self.log, self.closed = name, log, False

    def close(self):
        self.closed = True
        self.log.append(("close", self.name))


class _FakeMido:
    """Enough of mido to exercise port resolution, with a real Windows
    quirk: the same device is named differently in each direction."""

    def __init__(self, ins, outs, break_output=False):
        self.ins, self.outs, self.break_output = ins, outs, break_output
        self.log = []
        self.open_handles = []

    def get_input_names(self):
        return list(self.ins)

    def get_output_names(self):
        return list(self.outs)

    def _open(self, name, names, kind):
        if name not in names:
            raise OSError(f"unknown port {name!r}")
        h = _Handle(name, self.log)
        self.log.append((kind, name))
        self.open_handles.append(h)
        return h

    def open_input(self, name):
        return self._open(name, self.ins, "in")

    def open_output(self, name):
        if self.break_output:
            raise OSError(f"unknown port {name!r}")
        return self._open(name, self.outs, "out")


def test_windows_names_the_same_device_differently_each_way():
    """Input "X-Touch 0" and output "X-Touch 1" are one device - opening
    the output with the input's name is what failed in the field."""
    from xbridge import run as xrun

    mido = _FakeMido(ins=["X-Touch 0"], outs=["X-Touch 1"])
    runner = xrun.Runner(log=lambda *a: None)
    conns = runner._open_surfaces(mido, mido.get_input_names())
    assert len(conns) == 1
    assert ("in", "X-Touch 0") in mido.log
    assert ("out", "X-Touch 1") in mido.log


def test_a_failed_output_open_never_strands_the_input():
    """Windows MIDI is exclusive-access, so a leaked input handle makes
    every later retry fail too - the reconnect loop would dig its own
    hole instead of recovering."""
    from xbridge import run as xrun

    mido = _FakeMido(ins=["X-Touch 0"], outs=["X-Touch 1"],
                     break_output=True)
    runner = xrun.Runner(log=lambda *a: None)
    conns = runner._open_surfaces(mido, mido.get_input_names())
    assert conns == []
    assert all(h.closed for h in mido.open_handles), "input handle leaked"


def test_an_input_only_device_still_runs_one_way():
    from xbridge import run as xrun

    mido = _FakeMido(ins=["Some Controller 0"], outs=[])
    midi_in, midi_out = xrun._open_pair(mido, "Some Controller 0", "")
    assert midi_in is not None and midi_out is None


def test_base_name_ignores_the_backend_index():
    from xbridge import run as xrun

    assert xrun._base_name("X-Touch 0") == "x-touch"
    assert xrun._base_name("X-Touch INT 12") == "x-touch int"
    assert xrun._base_name("MPKmini3") == "mpkmini3"
    assert xrun._matching_port("X-Touch 0", ["Nope 0", "X-Touch 3"]) == "X-Touch 3"
    assert xrun._matching_port("X-Touch 0", []) == ""


def test_exact_output_match_is_preferred_over_index_pairing():
    from xbridge import run as xrun

    outs = ["X-Touch 1", "Other 0"]
    assert xrun._matching_port("X-Touch 0", outs) == "X-Touch 1"


# ---- outbound visibility (the "fader does nothing" question) ----------


def test_sends_are_counted_and_remembered():
    """"The fader does nothing" cannot be diagnosed unless the app can
    say what - if anything - it actually sent."""
    from xbridge import run as xrun

    class FakeSock:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

    r = xrun.Runner(log=lambda *a: None)
    sock = FakeSock()
    r._send(sock, osc.encode(osc.Message("/Page1/Fader201", (75,))))
    assert r.counters["sent"] == 1
    assert sock.sent[0][1] == r.ma3
    assert r.last_sent == ["/Page1/Fader201 75"]


def test_the_sent_history_stays_short():
    from xbridge import run as xrun

    class FakeSock:
        def sendto(self, data, addr):
            pass

    r = xrun.Runner(log=lambda *a: None)
    for i in range(30):
        r._send(FakeSock(), osc.encode(osc.Message(f"/x/{i}", (i,))))
    assert len(r.last_sent) == 8
    assert r.last_sent[-1] == "/x/29 29"
    assert r.counters["sent"] == 30


def test_test_send_reports_the_exact_message(monkeypatch):
    from xbridge import run as xrun

    r = xrun.Runner(log=lambda *a: None)
    line = r.test_send(strip=0, level=0.5)
    assert line == "/Page1/Fader201 50"
    assert r.counters["sent"] == 1


@pytest.mark.parametrize("form,check", [
    ("int100", lambda v: v == 50),
    ("float100", lambda v: isinstance(v, float) and abs(v - 50.0) < 0.01),
    ("float01", lambda v: isinstance(v, float) and abs(v - 0.5) < 0.001),
    ("int255", lambda v: v == 128),
])
def test_every_ma3_fader_value_form_is_available(form, check):
    """MA's manual says int 0-100, MA's own worked example sends float
    0-100, and FaderRange can move the top of the scale to 255. All four
    ship so the probe can find which one a console actually wants."""
    b = Bridge(config=_cfg(ma3_value=form))
    (d,) = b.midi_in(mcu.fader_out(0, 0.5))
    (value,) = osc.decode(d).args
    assert check(value), f"{form} produced {value!r}"


def test_shipped_defaults_match_a_stock_console():
    """A stock MA3 OSC line has an EMPTY prefix - the manual's receive
    example says so outright ("no prefix is defined") - and its canonical
    input example is "/Page1/Fader201,i,100". The shipped defaults have
    to match that, because it is the only configuration a user gets
    without editing the console first."""
    (d,) = Bridge().midi_in(mcu.fader_out(0, 1.0))
    msg = osc.decode(d)
    assert msg.address == "/Page1/Fader201"
    (value,) = msg.args
    assert isinstance(value, int) and not isinstance(value, bool)
    assert value == 100


# ---- the MA3 format probe ---------------------------------------------


def test_probe_covers_every_documented_dialect():
    """Prefix on/off crossed with all four value forms. Miss one and the
    probe can tell a user "none of these worked" when one would have."""
    from xbridge.probe import DIALECTS, Ma3Probe

    assert len(set(DIALECTS)) == len(DIALECTS), "a step is duplicated"
    from xbridge.probe import CMD_TEMPLATES
    assert {v for _, v, _ in DIALECTS} >= {"int100", "float100", "float01",
                                           "int255"}
    assert {v for _, v, _ in DIALECTS} & set(CMD_TEMPLATES) == set(CMD_TEMPLATES)
    assert {p for p, _, _ in DIALECTS} == {"", "gma3"}
    assert {a for _, _, a in DIALECTS} == {"page", "selected"}
    # The first step must be the stock console, so the common case is
    # answered before anyone stops watching.
    assert DIALECTS[0] == ("", "int100", "page")
    assert all(s.address for s in Ma3Probe().steps)


def test_probe_addresses_the_executor_it_was_asked_about():
    from xbridge.probe import Ma3Probe

    p = Ma3Probe(page=3, exec_=205)
    by_addr = {s.address for s in p.steps}
    assert "/Page3/Fader205" in by_addr          # stock
    assert "/gma3/Page3/Fader205" in by_addr     # prefixed
    assert "/Fader205" in by_addr                # selected page
    assert "/gma3/Fader205" in by_addr


def test_probe_sends_each_step_once_and_pauses_between():
    """A sweep with no gap is unwatchable: every step lands before a
    human can look up."""
    from xbridge.probe import Ma3Probe

    sent, slept = [], []

    class FakeSock:
        def sendto(self, data, addr):
            sent.append((osc.decode(data), addr))

    p = Ma3Probe(host="10.0.0.5", port=8001)
    p.run(dwell=1.5, sock=FakeSock(), sleep=slept.append)

    # two datagrams per step: zero first, then up, so the fader visibly
    # moves even if it was already sitting at the test level
    assert len(sent) == 2 * len(p.steps)
    assert all(addr == ("10.0.0.5", 8001) for _, addr in sent)
    assert slept == [0.5, 1.5] * len(p.steps)
    def level(m):
        a = m.args[0]
        return float(a.rsplit(" ", 1)[1]) if isinstance(a, str) else float(a)

    lows = [m for m, _ in sent[0::2]]
    assert all(level(m) == 0.0 for m in lows), "no step started at zero"
    sent = [(m, a) for m, a in sent[1::2]]
    forms = {(m.address, type(m.args[0]).__name__, str(m.args[0]))
             for m, _ in sent}
    assert len(forms) == len(p.steps), "two steps put the same bytes on the wire"


def test_probe_answer_can_be_kept():
    from xbridge.probe import DIALECTS, Ma3Probe

    p = Ma3Probe()
    from xbridge.probe import CMD_TEMPLATES

    for i, (prefix, value, addr_form) in enumerate(DIALECTS):
        cfg = p.apply(Config(), i)
        assert (cfg.prefix, cfg.ma3_addr) == (prefix, addr_form)
        if value in CMD_TEMPLATES:
            assert cfg.ma3_fader == "cmd"
            assert cfg.ma3_fader_cmd == CMD_TEMPLATES[value]
        else:
            assert (cfg.ma3_fader, cfg.ma3_value) == ("osc", value)


def test_probe_apply_keeps_the_rest_of_the_mapping(tmp_path, monkeypatch):
    """The winning dialect is two fields. Saving it must not throw away
    the executor numbers someone spent a show setting up."""
    from xbridge import app as xapp
    from xbridge import run as xrun

    store = tmp_path / "mapping.json"
    monkeypatch.setattr(xrun, "config_store_path", lambda: store)
    xrun.store_config({"target": "ma3", "prefix": "wrong",
                       "ma3_value": "int255", "fader_execs": [401, 402]})

    idx = 2                                     # gma3 + int100 + page
    out = xapp.api_probe_apply(index=idx)

    assert (out["prefix"], out["ma3_value"]) == ("gma3", "int100")
    kept = xrun.load_stored_config()
    assert kept.fader_execs == (401, 402)
    assert (kept.prefix, kept.ma3_value) == ("gma3", "int100")


# ---- MA3's real output format ------------------------------------------


def test_ma3_playback_feedback_is_not_the_input_format():
    """The bug this covers: MA3 does NOT echo /Page1/Fader201 back.

    Per "Object Playback Feedback", moving a fader on the console emits
    the object's enumerated address with an sif payload -
    /13.13.1.6.1,sif,"FaderMaster",3,63.5 - so a bridge that only listens
    for its own input format has a permanently dead return leg, and the
    motor faders never track the desk.
    """
    b = _plain(ma3_feedback={"13.13.1.6.1": 1})
    raw = b.osc_in(osc.encode(osc.Message(
        "/13.13.1.6.1", ("FaderMaster", 3, 63.5))))
    assert raw, "MA3's documented feedback format was ignored"
    ev = mcu.decode(raw[0])
    assert isinstance(ev, mcu.FaderMoved)
    assert ev.strip == 0
    assert ev.unit == pytest.approx(0.635, abs=0.01)


def test_master_playback_feedback_uses_the_same_shape():
    """Masters report as /13.12.X.Y with the same sif payload."""
    b = _plain(ma3_feedback={"13.12.3.1": 4})
    raw = b.osc_in(osc.encode(osc.Message(
        "/13.12.3.1", ("FaderMaster", 3, 100.0))))
    ev = mcu.decode(raw[0])
    assert ev.strip == 3 and ev.unit == pytest.approx(1.0, abs=0.01)


def test_key_playback_feedback_lights_the_button():
    b = _plain(ma3_feedback={"13.13.1.6.1": 1})
    raw = b.osc_in(osc.encode(osc.Message(
        "/13.13.1.6.1", ("Flash", 1, "Strobe 1 Cue 1"))))
    assert raw == [mcu.button_led(mcu.SELECT[0], True)]


def test_unmapped_playback_feedback_is_quietly_dropped():
    """Without a table entry there is nothing to move - but it must not
    be mistaken for a fader address either."""
    b = _plain()
    assert b.osc_in(osc.encode(osc.Message(
        "/13.13.1.6.9", ("FaderMaster", 3, 50.0)))) == []


def test_playback_feedback_survives_a_prefix():
    b = _plain(prefix="gma3", ma3_feedback={"13.13.1.6.1": 1})
    raw = b.osc_in(osc.encode(osc.Message(
        "/gma3/13.13.1.6.1", ("FaderMaster", 3, 50.0))))
    assert raw and isinstance(mcu.decode(raw[0]), mcu.FaderMoved)


def test_documented_encoder_mode_sends_a_relative_step():
    """MA3's /Encoder<x> takes a RELATIVE -100..100 percentage, not a
    level. Sending an absolute value there would jump the executor."""
    b = _plain(ma3_encoder="encoder")
    (d,) = b.midi_in(bytes((0xB0, 16, 1)))        # one tick right
    msg = osc.decode(d)
    assert msg.address == "/Page1/Encoder301"
    assert msg.args == (2,)                       # +2%, the step size
    (d,) = b.midi_in(bytes((0xB0, 16, 65)))       # one tick left
    assert osc.decode(d).args == (-2,)


def test_selected_page_address_form_drops_the_page_segment():
    """The escape hatch for an OSC line whose "Page" address cell has
    been renamed: /Fader201 applies to the selected page."""
    b = _plain(ma3_addr="selected", page=4)
    (d,) = b.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(d).address == "/Fader201"
    b2 = _plain(ma3_addr="selected", prefix="gma3")
    (d,) = b2.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(d).address == "/gma3/Fader201"


def test_command_line_address_is_documented_shape():
    """MA3 takes command line syntax on /cmd with a string - and that
    needs "Receive Command" enabled, separately from "Receive"."""
    b = _plain()
    (d,) = b.midi_in(mcu.fader_out(8, 1.0))
    msg = osc.decode(d)
    assert msg.address == "/cmd"
    assert isinstance(msg.args[0], str)
    b2 = _plain(prefix="gma3")
    (d,) = b2.midi_in(mcu.fader_out(8, 1.0))
    assert osc.decode(d).address == "/gma3/cmd"


def test_nothing_we_send_is_an_osc_bundle():
    """MA3: "OSC Bundle messages are currently not supported." A bundle
    would be dropped whole, silently."""
    from xbridge.probe import Ma3Probe
    from xbridge.targets import TARGETS, make_target

    out = [Ma3Probe().datagram(i) for i in range(len(Ma3Probe().steps))]
    b = _plain()
    out += b.hello()
    out += b.midi_in(mcu.fader_out(0, 0.5))
    out += b.midi_in(mcu.fader_out(8, 0.5))
    out += b.midi_in(bytes((0x90, mcu.SELECT[0], 127)))
    for d in out:
        if isinstance(d, bytes) and d.startswith(b"/") or isinstance(d, bytes):
            assert not d.startswith(b"#bundle"), d[:24]


# ---- console-side setup ------------------------------------------------


def test_setup_needs_two_lines_because_one_port_serves_both_directions():
    """MA3's OSC line has a single Port cell - "the port configuration is
    used for sending and receiving OSC data". So when the bridge sends to
    one port and listens on another, one line cannot do both, and looking
    for a send port and a receive port in the menu finds neither."""
    from xbridge import ma3setup

    lines = ma3setup.lines(send_port=8000, recv_port=9000)
    assert len(lines) == 2
    ports = [c[1] for ln in lines for c in ln.cells if c[0] == "Port"]
    assert ports == ["8000", "9000"]


def test_setup_collapses_to_one_line_when_no_feedback_is_wanted():
    from xbridge import ma3setup

    assert len(ma3setup.lines(send_port=8000, recv_port=0)) == 1


def test_setup_spells_out_the_switches_that_fail_silently():
    """Receive, Receive Command and Enable Input are three separate
    toggles, all off by default, each of which alone makes a correct
    bridge look dead."""
    from xbridge import ma3setup

    first = ma3setup.lines()[0]
    cells = {c[0]: c[1] for c in first.cells}
    assert cells["Receive"] == "Yes"
    assert cells["Receive Command"] == "Yes"
    assert cells["Mode"] == "UDP"
    assert cells["FaderRange"] == "100"
    assert any("Enable Input" == n for n, _, _ in ma3setup.GLOBAL_TOGGLES)


def test_the_sending_line_does_not_also_bind_the_bridges_port():
    """On one PC, a console line with Receive = Yes on the bridge's
    listen port fights the bridge for that port."""
    from xbridge import ma3setup

    out = ma3setup.lines(send_port=8000, recv_port=9000)[1]
    cells = {c[0]: c[1] for c in out.cells}
    assert cells["Send"] == "Yes"
    assert cells["Receive"] == "No"
    assert any("same PC" in w for w in ma3setup.warnings())


def test_setup_warns_when_both_ports_are_the_same():
    from xbridge import ma3setup

    warn = ma3setup.warnings(send_port=8000, recv_port=8000)
    assert any("8000" in w for w in warn)


def test_setup_carries_the_prefix_through_to_both_lines():
    """A prefix set in the mapping and not on the console is the silent
    failure this whole guide exists to prevent."""
    from xbridge import ma3setup

    for ln in ma3setup.lines(prefix="gma3"):
        assert any(c[0] == "Prefix" and c[1] == "gma3" for c in ln.cells)


def test_setup_endpoint_reflects_the_ports_it_is_given():
    from xbridge import app as xapp

    d = xapp.api_ma3_setup(host="10.0.0.9", send_port=8010, recv_port=9010,
                           bridge_ip="10.0.0.4")
    body = str(d)
    assert "8010" in body and "9010" in body and "10.0.0.4" in body
    assert d["warnings"] == []          # different machines: no port fight
    assert "13.13.1.6.1" in d["feedback"]


def test_x32_surface_never_emits_scribble_strip_sysex():
    """Not just at hello: a label arriving mid-show (the MA3 plugin
    pushes executor names) must not be forwarded either. The X32 ignores
    MCU LCD SysEx, and over DIN MIDI it is bandwidth that costs fader
    resolution."""
    from xbridge import surfaces, targets

    s = surfaces.X32MCSurface(Config(surface="x32mc"))
    assert s.render(targets.LabelFB(0, 0, "Exec 201"), targets) == []
    assert all(not m.startswith(b"\xf0") for m in s.hello([]))
    # everything else still works: it is a Mackie Control surface
    assert s.render(targets.FaderFB(2, 0.5), targets) == [mcu.fader_out(2, 0.5)]
    assert s.render(targets.ButtonFB("select", 0, True), targets) == \
        [mcu.button_led(mcu.SELECT[0], True)]
    assert s.decode(mcu.fader_out(1, 0.25)) == [mcu.decode(mcu.fader_out(1, 0.25))]


def test_the_command_line_route_bypasses_executor_addressing():
    """A second way to move the same fader. /cmd takes command-line
    syntax and never touches the OSC line's Fader or Page address cells,
    so it works when executor addressing does not - and it needs Receive
    Command rather than Receive, which is a different switch again."""
    b = _plain(ma3_fader="cmd")
    (d,) = b.midi_in(mcu.fader_out(0, 0.5))
    msg = osc.decode(d)
    assert msg.address == "/cmd"
    assert msg.args == ("FaderMaster 201 At 50.0",)
    # it follows the mapped executor like the OSC route does
    b2 = _plain(ma3_fader="cmd", fader_execs=(207,))
    (d,) = b2.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(d).args == ("FaderMaster 207 At 100.0",)
    # and the template is config, so a version wanting other syntax is a
    # setting rather than a code change
    b3 = _plain(ma3_fader="cmd", page=3, fader_execs=(207,),
                ma3_fader_cmd="FaderMaster Executor {page}.{exec} At {pct}")
    (d,) = b3.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(d).args == ("FaderMaster Executor 3.207 At 100.0",)


def test_the_command_line_route_honours_the_prefix():
    b = _plain(ma3_fader="cmd", prefix="gma3")
    (d,) = b.midi_in(mcu.fader_out(0, 0.5))
    assert osc.decode(d).address == "/gma3/cmd"


def test_the_probe_offers_the_command_line_route_last():
    """It is the fallback, not the first guess - executor addressing is
    what a stock console uses."""
    from xbridge.probe import DIALECTS, Ma3Probe

    from xbridge.probe import CMD_TEMPLATES

    cmd_steps = [i for i, (_, v, _) in enumerate(DIALECTS)
                 if v in CMD_TEMPLATES]
    # they come last: executor addressing is what a stock console uses
    assert cmd_steps == list(range(len(DIALECTS) - len(cmd_steps),
                                   len(DIALECTS)))
    steps = Ma3Probe(exec_=205).steps
    lines = {steps[i].line for i in cmd_steps}
    assert "/cmd FaderMaster 205 At 75.0" in lines
    assert "/cmd FaderMaster Executor 1.205 At 75.0" in lines
    assert "/cmd FaderMaster Page 1.205 At 75.0" in lines
    assert "/gma3/cmd FaderMaster 205 At 75.0" in lines
    # the confirmed-working spelling is tried first of the command forms
    assert steps[cmd_steps[0]].line == "/cmd FaderMaster 205 At 75.0"
    assert all(s.address for s in steps)


def test_keeping_a_command_line_step_switches_the_route():
    from xbridge.probe import DIALECTS, Ma3Probe

    idx = next(i for i, (p, v, _) in enumerate(DIALECTS)
               if v == "cmd2" and p == "")
    cfg = Ma3Probe().apply(Config(), idx)
    assert cfg.ma3_fader == "cmd"
    assert cfg.ma3_fader_cmd == "FaderMaster {exec} At {pct}"
    assert cfg.prefix == ""


def test_the_default_command_line_syntax_is_the_one_that_works():
    """Confirmed on a 2.4.2 console: "FaderMaster 201 At 50". MA's OSC
    page prints "FaderMaster Page 1.201 At 50" instead, and that form is
    answered with IllegalProperty."""
    b = _plain(ma3_fader="cmd")
    (d,) = b.midi_in(mcu.fader_out(0, 0.5))
    msg = osc.decode(d)
    assert msg.address == "/cmd"
    assert msg.args == ("FaderMaster 201 At 50.0",)
    assert "Page" not in msg.args[0]


def test_the_command_line_route_follows_the_bank_only_when_told_how():
    """The bare form addresses the console's CURRENT page, so the bank
    buttons would silently drive the wrong executors. A page command
    fixes that - but only a spelling the user has verified ships as
    active behaviour, since guessing syntax is what broke the fader."""
    quiet = _plain(ma3_fader="cmd")
    quiet.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))
    assert quiet.target.set_page(2) == []

    told = _plain(ma3_fader="cmd", ma3_page_cmd="Select Page {page}")
    (d,) = _wire_one(told.target.set_page(4))
    assert osc.decode(d) == osc.Message("/cmd", ("Select Page 4",))

    # the OSC route names the page in every address, so it sends nothing
    osc_route = _plain(ma3_page_cmd="Select Page {page}")
    assert osc_route.target.set_page(4) == []


def _wire_one(messages):
    return [osc.encode(m) for m in messages]


# ---- holding position, and learning the console's feedback --------------


def test_a_fader_move_is_echoed_back_so_the_motor_holds():
    """A motorised MCU surface holds the value the DAW last told it. A
    bridge that says nothing leaves the surface believing the old value,
    and the motor drags the fader back the moment the hand comes off."""
    b = _plain(surface="x32mc")
    raw = mcu.fader_out(2, 0.6)
    (echo,) = b.echo_for(raw)
    ev = mcu.decode(echo)
    assert isinstance(ev, mcu.FaderMoved)
    assert ev.strip == 2
    assert ev.unit == pytest.approx(0.6, abs=0.01)


def test_the_echo_is_not_suppressed_by_touch():
    """Touch suppression stops the CONSOLE fighting a hand. The echo is
    the surface's own value coming back, so it cannot fight anything -
    and suppressing it is exactly when it is needed, because a hand is
    on the fader."""
    b = _plain()
    b.midi_in(bytes((0x90, 104, 127)))          # finger down on strip 1
    assert b.echo_for(mcu.fader_out(0, 0.8)), "echo suppressed while touched"


def test_the_echo_can_be_turned_off():
    b = _plain(local_echo=False)
    assert b.echo_for(mcu.fader_out(0, 0.5)) == []


def test_only_faders_are_echoed():
    """Buttons and encoders have their own state on the surface; echoing
    them would fight the LED feedback the console sends."""
    b = _plain()
    assert b.echo_for(bytes((0x90, mcu.SELECT[0], 127))) == []
    assert b.echo_for(bytes((0xB0, 16, 1))) == []


def test_unmapped_console_feedback_is_remembered_for_learning():
    """MA3 reports playback by pool index, never by executor, so which
    object drives which motor is knowledge only the user has. Dropping
    those messages silently left nothing to map from."""
    b = _plain()
    b.osc_in(osc.encode(osc.Message("/13.13.1.6.1", ("FaderMaster", 3, 63.5))))
    b.osc_in(osc.encode(osc.Message("/13.12.3.1", ("FaderMaster", 3, 10.0))))
    assert b.target.unmapped == {"13.13.1.6.1": ("FaderMaster", 63.5),
                                 "13.12.3.1": ("FaderMaster", 10.0)}


def test_learning_does_not_grow_without_bound():
    b = _plain()
    for i in range(60):
        b.osc_in(osc.encode(osc.Message(f"/13.13.1.6.{i}",
                                        ("FaderMaster", 3, 1.0))))
    assert len(b.target.unmapped) <= 16


def test_a_mapped_address_drives_the_motor_and_leaves_the_learn_list():
    b = _plain(ma3_feedback={"13.13.1.6.1": 3})
    raw = b.osc_in(osc.encode(osc.Message("/13.13.1.6.1",
                                          ("FaderMaster", 3, 50.0))))
    assert mcu.decode(raw[0]).strip == 2          # strip 3 is index 2
    assert "13.13.1.6.1" not in b.target.unmapped


def test_learning_an_address_takes_effect_without_a_restart(tmp_path,
                                                            monkeypatch):
    from xbridge import app as xapp
    from xbridge import run as xrun

    monkeypatch.setattr(xrun, "config_store_path",
                        lambda: tmp_path / "map.json")
    xrun.store_config({"target": "ma3", "fader_execs": [401, 402]})

    class FakeRunner:
        def __init__(self):
            self.bridge = Bridge(config=_cfg())

    monkeypatch.setattr(xapp, "_runner", FakeRunner())
    out = xapp.api_feedback_learn(addr="/13.13.1.6.1", strip=2)

    assert out["strip"] == 2 and out["addr"] == "13.13.1.6.1"
    # persisted, without losing the rest of the mapping
    kept = xrun.load_stored_config()
    assert kept.ma3_feedback == {"13.13.1.6.1": 2}
    assert kept.fader_execs == (401, 402)
    # and live on the running bridge
    b = xapp._runner.bridge
    raw = b.osc_in(osc.encode(osc.Message("/13.13.1.6.1",
                                          ("FaderMaster", 3, 100.0))))
    assert raw and mcu.decode(raw[0]).strip == 1


def test_learning_refuses_nonsense():
    from xbridge import app as xapp

    for bad in ("", "not.an.address", "/Page1/Fader201"):
        try:
            xapp.api_feedback_learn(addr=bad, strip=1)
        except Exception as exc:
            assert "pool address" in str(exc)
        else:
            raise AssertionError(f"accepted {bad!r}")


def test_a_mapping_can_be_seen_and_undone(tmp_path, monkeypatch):
    """Once an address was claimed it left the "not mapped yet" list and
    never came back, so a wrong strip was a one-way door with nothing on
    screen to show what any strip was following."""
    from xbridge import app as xapp
    from xbridge import run as xrun

    monkeypatch.setattr(xrun, "config_store_path",
                        lambda: tmp_path / "map.json")
    xrun.store_config({"target": "ma3"})

    class FakeRunner:
        def __init__(self):
            self.bridge = Bridge(config=_cfg())
            self.state, self.detail, self.midi_name = "running", "", ""
            self.counters, self.last_sent = {}, []
            self.last_midi, self.last_osc, self.no_output = [], [], []

    monkeypatch.setattr(xapp, "_runner", FakeRunner())
    monkeypatch.setattr(xapp, "_thread", None)

    xapp.api_feedback_learn(addr="14.14.1.6.1", strip=1)
    assert xapp.api_status()["feedback_map"] == {"14.14.1.6.1": 1}

    # forget it: strip 0
    out = xapp.api_feedback_learn(addr="14.14.1.6.1", strip=0)
    assert out["map"] == {}
    assert xrun.load_stored_config().ma3_feedback == {}
    assert xapp._runner.bridge.config.ma3_feedback == {}

    # and it is offered for mapping again the next time it arrives
    b = xapp._runner.bridge
    b.osc_in(osc.encode(osc.Message("/14.14.1.6.1", ("FaderMaster", 3, 5.0))))
    assert "14.14.1.6.1" in b.target.unmapped


def test_remapping_to_another_strip_replaces_rather_than_duplicates():
    from xbridge import app as xapp

    class FakeRunner:
        def __init__(self):
            self.bridge = Bridge(config=_cfg())

    import xbridge.run as xrun_mod
    saved = []
    xapp._runner = FakeRunner()
    try:
        real = xrun_mod.store_config
        xrun_mod.store_config = lambda body: saved.append(body) or Config()
        xapp.api_feedback_learn(addr="14.14.1.6.1", strip=1)
        out = xapp.api_feedback_learn(addr="14.14.1.6.1", strip=5)
    finally:
        xrun_mod.store_config = real
        xapp._runner = None
    assert out["map"] == {"14.14.1.6.1": 5}
