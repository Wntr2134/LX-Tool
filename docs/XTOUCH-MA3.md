# X-Touch → grandMA3 onPC Bridge — Setup Guide & Capabilities

For the **full-size Behringer X-Touch** and **grandMA3 onPC on Windows**
(also works on Mac). Part of LX-Tool: `lx xtouch`.

---

## What you need

- Behringer X-Touch (full size), USB cable
- grandMA3 onPC running on the same PC (or same network)
- Python 3.9+ on the PC
- LX-Tool with the MIDI extra:

```
pip install "lx-tool[xtouch]"
```

---

## Step 1 — Put the X-Touch in MC mode

1. Power the X-Touch **off**.
2. **Hold the channel-1 SELECT button** and power it **on** — the config
   screen appears.
3. **Encoder 1** → set operation mode to **MC**
4. **Encoder 2** → set interface to **USB**
5. Press channel-1 **SELECT** again to save, then power-cycle.

(The full-size unit also offers HUI and network/Xctl modes — the bridge
needs **MC over USB**.)

## Step 2 — Prove the surface works (no MA3 needed)

```
lx xtouch test
```

All nine faders sweep up and down, the encoder rings fill, the scribble
strips say "LX-Tool". **If this works, the X-Touch side is 100% good.**
If nothing moves: wrong mode (redo step 1) or a USB/driver issue.

## Step 3 — Set up grandMA3 onPC

Menu → **In & Out → OSC** → add a line:

| Setting | Value |
|---|---|
| Destination IP | `127.0.0.1` (same PC) or the bridge PC's IP |
| Port (send) | **9000** |
| Port (receive) | **8000** |
| Send | **On** |
| Receive | **On** |
| Prefix | leave empty |
| Feedback filter | enable **executor** feedback |

## Step 4 — Run the bridge

```
lx xtouch run
```

MA3 on another machine on the network:

```
lx xtouch run --host 192.168.1.20
```

Leave the window open; Ctrl-C stops it.

---

## Exact capabilities (default mapping)

| X-Touch control | Does what in MA3 | Feedback to the surface |
|---|---|---|
| **Faders 1–8** (motorised, touch-sensing) | Executor faders **201–208** on the current page | **Motors physically follow MA3** — cue fades, other operators, anything |
| **Master fader** (9th) | **Grand master** (command line) | Follows if you map it to an executor in config |
| **SELECT row** (8 buttons) | Executor keys **101–108** — real press *and* release, so flash/temp buttons behave properly | Button LED lights when the executor is on |
| **MUTE row** (8 buttons) | Executor keys **291–298** | LED feedback |
| **Push-encoders 1–8** (turn) | Executor faders **301–308**, relative — 2% per click by default | **LED ring shows the level** |
| **FADER BANK ◀ / ▶** (also CHANNEL ◀ ▶) | MA3 **page down / up** — all 8 strips retarget to the new page | Strips re-label |
| **Scribble strips** (8 LCDs) | — | Show executor number + current page |
| **Fader touch** | — | While your finger is on a fader, MA3 feedback for that strip is **ignored** so the motor never fights your hand; it snaps to MA3's value on release |

### Behaviour guarantees

- Bidirectional: the surface is a *mirror* of MA3, not a one-way remote.
- Feedback for pages other than the current one is ignored — page changes
  never scramble the faders.
- Junk on the OSC port (other software, wrong sender) is dropped, never a
  crash.

### Every mapping is editable

```
lx xtouch config -o mymap.json
```

Open `mymap.json` in any editor and change: which executors the faders,
buttons and encoders hit; the starting page; the encoder sensitivity;
the OSC prefix (must match MA3's if you set one there). Then:

```
lx xtouch run --config mymap.json
```

### Not mapped (yet)

Jog wheel, transport buttons, the REC row, the assignment 7-segment
display, and the function-key rows are currently unmapped. The plumbing
supports them — say what you want them to do and they can be added.

---

## Current status — read this

The protocol code is fully tested in software, but this has **not yet had
its first run against a physical X-Touch and MA3**. MA3's OSC address
format has changed between versions, so the first session may need a
small config tweak (that's why everything is configurable). What to
report from the first try:

1. Did `lx xtouch test` move the faders? (proves the X-Touch side)
2. Does moving fader 1 change executor 201 in MA3? (proves bridge → MA3)
3. Does moving executor 201 on-screen move fader 1's motor? (proves
   MA3 → bridge)

Whichever of those three fails (if any) pinpoints the fix immediately.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no X-Touch found` | It's not in MC/USB mode (step 1), or another app has grabbed the MIDI port — close your DAW |
| `MIDI support is not installed` | `pip install "lx-tool[xtouch]"` |
| Test works, MA3 doesn't react | MA3 OSC line: Receive on, port 8000, destination IP correct, prefix matches (empty ↔ empty) |
| MA3 reacts, motors don't follow | MA3 OSC line: **Send** on, port 9000, executor feedback enabled in the filter |
| Faders move the wrong executors | Different width/user profile — edit `fader_execs` in the config to your actual executor numbers |
