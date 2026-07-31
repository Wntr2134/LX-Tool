# Behringer X-Touch → grandMA3 onPC bridge

Your full-size X-Touch as a native MA3 surface: motorised faders drive
executors **and follow them back**, buttons hit executor keys with LED
feedback, the push-encoders turn rotary executors with ring feedback, and
the scribble strips label what each strip is patched to. No MIDI-learn, no
third-party licence — it speaks the X-Touch's Mackie Control protocol on
one side and MA3's own OSC on the other.

## Install

```bash
pip install "lx-tool[xtouch]"
```

(That adds `mido` + `python-rtmidi` for MIDI; everything else is built in.)

## Set up the X-Touch

1. Connect via USB.
2. Put it in **MC mode**: hold the channel-1 **SELECT** while powering on,
   choose `MC`, and `USB` as the interface.
3. Prove it works before involving MA3:

```bash
lx xtouch test
```

Faders sweep, encoder rings fill, the strips say hello. If nothing moves,
it's cabling or mode — fix that first.

## Set up grandMA3 onPC

Menu → **In & Out → OSC**, add a line:

| Setting | Value |
|---|---|
| Destination IP | the machine running the bridge (`127.0.0.1` if the same PC) |
| Port (send) | `9000` |
| Port (receive) | `8000` |
| Send / Receive | both **on** |
| Prefix | leave empty (or set one and give the bridge the same) |
| Feedback | enable executor feedback so the motors follow MA3 |

## Run it

```bash
lx xtouch run
# elsewhere on the network:
lx xtouch run --host 192.168.1.20
```

## What's mapped (defaults)

| Surface | MA3 |
|---|---|
| Faders 1–8 | Executors **201–208** on the current page |
| Master fader (9th) | Grand master (via command line) |
| SELECT row | Executor keys **101–108** (press/release, LED follows) |
| MUTE row | Executor keys **291–298** |
| Push-encoders 1–8 | Executors **301–308** (relative, ring shows level) |
| FADER BANK ◀ ▶ | MA3 page down / up |
| Scribble strips | Executor number + page |

Change any of it:

```bash
lx xtouch config -o mymap.json
# edit the executor numbers / prefix / encoder step
lx xtouch run --config mymap.json
```

## Behaviour worth knowing

- **The motors never fight your hand.** While a fader is touched, MA3
  feedback for that strip is ignored; it snaps to MA3's value on release.
- Feedback for pages other than the current one is ignored, so page
  changes don't scramble the surface.
- Garbage on the OSC port (other tools, wrong sender) is dropped, never
  fatal.

## Honest status

The protocol layers (Mackie Control and OSC) are fully unit-tested, but
this has **not yet been run against a physical X-Touch + MA3 onPC** — that
first hardware session may surface an MA3 OSC address quirk or an MC-mode
detail to adjust. Both ends are configurable precisely so those fixes are
config edits, not rewrites. Run `lx xtouch test` first, then `run`, and
report what happens — fader moves in both directions are the key thing.
