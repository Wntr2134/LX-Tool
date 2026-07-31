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
