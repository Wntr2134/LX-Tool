"""The exact grandMA3 OSC configuration for a given bridge setup.

MA3's OSC line has ONE Port cell, and the manual is explicit that "the
port configuration is used for sending and receiving OSC data" - with the
hint that "if you want to use different ports for sending and receiving,
you can create multiple configuration lines". Identical wording in 2.1,
2.2 and 2.3.

That single fact is why looking for a send port and a receive port in the
OSC menu is a dead end, and why one line can never be a complete
round trip when the bridge listens on a different port from the one it
sends to. It needs two lines: one that receives on the console's port,
and one that sends to the bridge's.

Rather than describe that and let everyone translate it to their own
ports, this generates the rows to type, with their numbers already in.
"""

from __future__ import annotations

from dataclasses import dataclass

# Menu -> In & Out -> OSC. These two are above the grid and are not part
# of any line; both are off until someone turns them on.
GLOBAL_TOGGLES = (
    ("Enable Input", "On",
     "Off by default. Nothing is received until this is on."),
    ("Enable Output", "On, if you want the motors to follow the desk",
     "Only needed for feedback."),
)


@dataclass
class Line:
    """One OSCData row, and why each cell is what it is."""

    title: str
    why: str
    cells: tuple

    def as_dict(self) -> dict:
        return {"title": self.title, "why": self.why,
                "cells": [{"name": n, "value": v, "note": note}
                          for n, v, note in self.cells]}


def lines(*, host: str = "127.0.0.1", send_port: int = 8000,
          recv_port: int = 9000, prefix: str = "",
          bridge_ip: str = "127.0.0.1") -> list[Line]:
    """The OSC lines to create for this bridge configuration.

    ``send_port`` is the port the bridge sends to (the console's ear).
    ``recv_port`` is the port the bridge listens on (the console must
    send there). They differ, so two lines are needed.
    """
    same_pc = host in ("127.0.0.1", "localhost", "::1")
    pfx = prefix or "(leave empty)"
    out = [Line(
        title=f"Line 1 - the console listens on {send_port}",
        why="This is the one that makes faders work. Without it the "
            "bridge is talking to a port nobody is listening on.",
        cells=(
            ("Name", "XBridge in", "anything you like"),
            ("Mode", "UDP", "the bridge speaks UDP, not TCP"),
            ("Port", str(send_port),
             "must equal the bridge's send port"),
            ("Destination IP", bridge_ip if not same_pc else "127.0.0.1",
             "only used for sending; harmless here"),
            ("Prefix", pfx,
             "must match the bridge exactly - a mismatch is discarded "
             "in silence"),
            ("Receive", "Yes",
             "NOT on by default, and separate from Enable Input"),
            ("Receive Command", "Yes",
             "separate again - the master fader and transport keys use "
             "/cmd and are dead without it"),
            ("Send", "No", "line 2 does the sending"),
            ("Page / Fader / Key", "Page / Fader / Key",
             "the defaults; the bridge speaks /Page1/Fader201"),
            ("FaderRange", "100",
             "255 would make every level read low"),
            ("EchoInput", "Yes while testing",
             "shows arriving messages in the System Monitor - the "
             "fastest way to see whether anything lands"),
        ))]
    if recv_port and recv_port != send_port:
        out.append(Line(
            title=f"Line 2 - the console sends to {recv_port}",
            why="Only needed for feedback: motor faders, button LEDs. "
                "A second line exists because one line's Port serves "
                "both directions, so it cannot send anywhere else.",
            cells=(
                ("Name", "XBridge out", "anything you like"),
                ("Mode", "UDP", ""),
                ("Port", str(recv_port),
                 "must equal the bridge's listen port"),
                ("Destination IP", bridge_ip,
                 "the machine running the bridge"),
                ("Prefix", pfx, "same as line 1"),
                ("Send", "Yes", "this is the sending line"),
                ("Receive", "No",
                 "leave this off. With Receive on, the console also "
                 "binds this port - and on one PC it would be fighting "
                 "the bridge for it"),
                ("Send Command", "No", "not needed"),
                ("EchoOutput", "Yes while testing",
                 "shows what the console emits, which is how you find "
                 "the addresses for the feedback map"),
            )))
    return out


def warnings(*, host: str = "127.0.0.1", send_port: int = 8000,
             recv_port: int = 9000) -> list[str]:
    """Setups that are known to bite, given these numbers."""
    out = []
    same_pc = host in ("127.0.0.1", "localhost", "::1")
    if send_port == recv_port:
        out.append(
            f"The bridge sends to {send_port} and listens on the same "
            "port. On one PC only one program can hold a UDP port, so "
            "the console and the bridge would be fighting over it. Give "
            "the bridge a different listen port.")
    if same_pc:
        out.append(
            "Console and bridge are on the same PC, so line 2 must have "
            "Receive = No. Otherwise MA3 tries to bind the bridge's "
            "listen port and one of the two loses it.")
    return out


def feedback_note() -> str:
    return ("Motor faders need one more thing. MA3 does not echo "
            "/Page1/Fader201 back - playback feedback arrives addressed "
            "by pool index, e.g. /13.13.1.6.1 ,sif, \"FaderMaster\",3,63.5. "
            "Turn EchoOutput on, move a fader on the console, read the "
            "address out of the System Monitor, and put it in the "
            "mapping's ma3_feedback as {\"13.13.1.6.1\": 1} to drive "
            "strip 1.")
