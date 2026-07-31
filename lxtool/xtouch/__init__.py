"""Behringer X-Touch (full size) as a native surface for grandMA3 onPC.

The X-Touch speaks Mackie Control (MCU) over USB-MIDI: nine motorised
faders, eight push-encoders with LED rings, scribble-strip LCDs and a grid
of lit buttons. grandMA3 onPC listens and talks OSC. This package is the
bridge between the two - faders drive executors and follow them back,
buttons hit executor keys, encoders turn rotary executors, and the strips
show what they are patched to.
"""
