"""The X-Touch <-> grandMA3 bridge, tested with no hardware in the room.

MCU codec, OSC codec, and the mapping in between are all pure functions,
so a fader move can be traced from MIDI bytes to the OSC datagram MA3
receives - and back from MA3's feedback to the motor-fader message.
"""

from __future__ import annotations

import pytest

from lxtool.xtouch import mcu, osc
from lxtool.xtouch.bridge import Bridge, Config
from lxtool.xtouch.run import default_config_json, find_xtouch_port, load_config

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
    b = Bridge()
    (datagram,) = b.midi_in(mcu.fader_out(0, 0.75))
    msg = osc.decode(datagram)
    assert msg.address == "/Page1/Fader201"
    assert msg.args == (75,)


def test_prefix_and_page_shape_the_address():
    b = Bridge(config=Config(prefix="gMA3", page=3))
    (datagram,) = b.midi_in(mcu.fader_out(1, 1.0))
    assert osc.decode(datagram).address == "/gMA3/Page3/Fader202"


def test_master_fader_uses_the_command_line():
    b = Bridge()
    (datagram,) = b.midi_in(mcu.fader_out(8, 0.8))
    msg = osc.decode(datagram)
    assert msg.address == "/cmd"
    assert "Master 2.1 At 80" in msg.args[0]


def test_select_button_press_and_release_hit_the_key():
    b = Bridge()
    (down,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 127)))
    (up,) = b.midi_in(bytes((0x90, mcu.SELECT[0], 0)))
    assert osc.decode(down) == osc.Message("/Page1/Key101", (1,))
    assert osc.decode(up) == osc.Message("/Page1/Key101", (0,))


def test_encoder_ticks_accumulate_and_clamp():
    b = Bridge()
    for _ in range(3):
        (d,) = b.midi_in(bytes((0xB0, 16, 1)))
    assert osc.decode(d).args == (6,)          # 3 ticks * 2% = 6
    for _ in range(100):
        (d,) = b.midi_in(bytes((0xB0, 16, 65)))
    assert osc.decode(d).args == (0,)          # clamped at the bottom


def test_bank_buttons_flip_the_page():
    b = Bridge()
    b.midi_in(bytes((0x90, mcu.FADER_BANK_RIGHT, 127)))
    assert b.config.page == 2
    (datagram,) = b.midi_in(mcu.fader_out(0, 1.0))
    assert osc.decode(datagram).address == "/Page2/Fader201"
    b.midi_in(bytes((0x90, mcu.FADER_BANK_LEFT, 127)))
    b.midi_in(bytes((0x90, mcu.FADER_BANK_LEFT, 127)))
    assert b.config.page == 1                  # never below page 1


# ---- the bridge, MA3 -> surface ---------------------------------------


def test_ma3_fader_feedback_moves_the_motor():
    b = Bridge()
    (raw,) = b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (50,))))
    ev = mcu.decode(raw)
    assert isinstance(ev, mcu.FaderMoved)
    assert ev.strip == 0
    assert ev.unit == pytest.approx(0.5, abs=0.01)


def test_feedback_never_fights_a_touched_fader():
    b = Bridge()
    b.midi_in(bytes((0x90, 104, 127)))         # finger down on strip 1
    assert b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (99,)))) == []
    b.midi_in(bytes((0x90, 104, 0)))           # finger off
    assert b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (99,)))) != []


def test_feedback_for_another_page_is_ignored():
    b = Bridge()
    assert b.osc_in(osc.encode(osc.Message("/Page9/Fader201", (50,)))) == []


def test_key_feedback_lights_the_button_led():
    b = Bridge()
    (raw,) = b.osc_in(osc.encode(osc.Message("/Page1/Key101", (1,))))
    assert raw == mcu.button_led(mcu.SELECT[0], True)


def test_encoder_feedback_paints_the_ring():
    b = Bridge()
    (raw,) = b.osc_in(osc.encode(osc.Message("/Page1/Fader301", (100,))))
    assert raw[0] == 0xB0 and raw[1] == 48     # ring CC for encoder 1


def test_float_and_junk_feedback_are_safe():
    b = Bridge()
    (raw,) = b.osc_in(osc.encode(osc.Message("/Page1/Fader201", (50.0,))))
    assert mcu.decode(raw).unit == pytest.approx(0.5, abs=0.01)
    assert b.osc_in(b"\x01\x02not osc") == []
    assert b.osc_in(osc.encode(osc.Message("/Page1/FaderNaN", (1,)))) == []


def test_hello_labels_the_strips():
    payloads = b"".join(Bridge().hello())
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
    b = Bridge()
    (down,) = b.midi_in(bytes((0x90, mcu.PLAY, 127)))
    assert osc.decode(down) == osc.Message("/cmd", ("Go+",))
    assert b.midi_in(bytes((0x90, mcu.PLAY, 0))) == []       # release: nothing
    (stop,) = b.midi_in(bytes((0x90, mcu.STOP, 127)))
    assert osc.decode(stop).args == ("Pause",)
    (rew,) = b.midi_in(bytes((0x90, mcu.REWIND, 127)))
    assert osc.decode(rew).args == ("Go-",)


def test_unmapped_transport_button_stays_silent():
    b = Bridge()
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

    from lxtool.xtouch.run import Runner, midi_available

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
    from lxtool.xtouch.run import Runner, midi_available

    if not midi_available():
        pytest.skip("mido not installed here")
    r = Runner(recv_port=0, midi_port="", log=lambda *a: None)
    r.stop()                      # stopped before it starts: run returns fast
    assert r.run() == 0
    assert r.state == "stopped"


def test_sniffer_formatting_is_readable_and_junk_proof():
    from lxtool.xtouch.run import format_midi, format_osc

    line = format_osc(osc.encode(osc.Message("/Page1/Fader201", (75,))))
    assert "/Page1/Fader201" in line and "75" in line
    assert "undecodable" in format_osc(b"\x01\x02\x03")
    assert "FaderMoved" in format_midi(mcu.fader_out(0, 1.0))
    assert "??" in format_midi(b"\xfe")


def test_web_status_endpoint_reports_without_midi_installed():
    from lxtool.web import app as web

    d = web.api_xtouch_status()
    assert set(d) >= {"available", "running", "state", "detail"}
    assert d["running"] is False


# ---- the X32 audio target ---------------------------------------------


def _x32() -> Bridge:
    return Bridge(config=Config(target="x32"))


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
    from lxtool.xtouch import targets

    with pytest.raises(ValueError):
        targets.make_target(Config(target="hog4"))


# ---- MagicQ ------------------------------------------------------------


def _magicq() -> Bridge:
    return Bridge(config=Config(target="magicq"))


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
    return Bridge(config=Config(target="resolume"))


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
    return Bridge(config=Config(target="companion"))


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


# ---- the stored mapping (what the editor UI reads and writes) ---------


def test_stored_mapping_roundtrip(tmp_path, monkeypatch):
    from lxtool.xtouch.run import load_stored_config, store_config

    monkeypatch.setenv("LXTOOL_XTOUCH", str(tmp_path / "xtouch.json"))
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

    from lxtool.web import app as web

    monkeypatch.setenv("LXTOOL_XTOUCH", str(tmp_path / "xtouch.json"))
    d = web.api_xtouch_config()
    assert d["config"]["target"] == "ma3"
    assert d["config"]["fader_execs"] == list(range(201, 209))

    class FakeRequest:
        async def json(self):
            return {"target": "x32", "page": 2}

    asyncio.run(web.api_xtouch_config_save(FakeRequest()))
    assert web.api_xtouch_config()["config"]["target"] == "x32"
    assert jsonlib.loads((tmp_path / "xtouch.json").read_text())["page"] == 2
