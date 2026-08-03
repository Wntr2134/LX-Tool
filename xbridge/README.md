# X-Touch Bridge — Setup Guide & Capabilities

For the **full-size Behringer X-Touch**, driving either **grandMA3 onPC**
(lighting) or a **Behringer X32 / Midas M32** (audio). Part of LX-Tool:
`lx xtouch`, or section 7 of the desktop app.

## Targets

| Target | What the surface becomes |
|---|---|
| `ma3` (default) | Executor wing for grandMA3 onPC — see the mapping below |
| `ma2` | **grandMA2** via its **Web Remote websocket** (MA2 has no OSC — this is the same route ShowCockpit uses). Faders ride `Executor <page>.1–8` through the console command line, SELECT = Go, MUTE = Off, encoders ride executors 9–16, master = SpecialMaster 2.1 (template, editable), PLAY/STOP/REW = Go/Pause/GoBack on the first executor. Executor **titles and levels stream back** from the web remote to label the strips and drive the motors — *experimental: the feedback format is reverse-engineered, not documented by MA*. Setup: enable Remotes on the console (Setup → Console → Global Settings → "Login enabled"), confirm the browser web remote works, then give the bridge the same user/password (default `remote`/`remote`) in the mapping editor. Port 80 |
| `magicq` | Playback wing for ChamSys MagicQ: faders ride playbacks 1–8 with motor feedback (MagicQ's own `/feedback/pb+exec` stream), SELECT = **Go**, MUTE = a true **Flash** (in on press, out on release), encoders drive execute grid 1, master fader rides playback 9, STOP = blackout on / PLAY = blackout off. Enable OSC in MagicQ: Setup → View Settings → Network, receive 8000 / transmit 9000 |
| `x32` | Fader wing for an X32/M32: faders are channel levels (motors follow the desk), MUTE is the real mute with LED state, SELECT selects the channel, encoders are pan with ring feedback, master fader is the main stereo bus, FADER BANK pages through ch 1–8 / 9–16 / 17–24 / 25–32, and the scribble strips show the console's **own channel names** |
| `resolume` | Media wing for Resolume Arena/Avenue: faders are **layer opacity** (banked 8 layers at a time), MUTE bypasses the layer, SELECT connects the matching column, encoders ride the layer masters, master fader is the composition master. For motor feedback enable OSC *output* in Resolume's preferences at this machine's listen port |
| `eos` | Fader wing for the **ETC Eos family**: the bridge creates OSC fader bank 1 on connect, faders ride it as floats with motor feedback (Eos echoes an OSC-moved fader after ~3s — its behaviour, not a fault), SELECT = the fader's Fire, MUTE = its Stop, PLAY/STOP = master Go and Stop/Back. Eos setup: Setup → System → Show Control → OSC, UDP RX 8000, TX at this machine :9000 |
| `companion` | A surface for **Bitfocus Companion** — and through it, the hundreds of things Companion controls. SELECT row presses buttons on row 0 of the current Companion page, MUTE row presses row 1, transport keys press row 2 (REW FF STOP PLAY REC = columns 0–4), all with true down/up so latch and momentary both behave. Faders write custom variables `fader1`–`fader8` and `master` as 0–100 for use in any Companion action; encoders send real rotate-left/right events on row 3; FADER BANK changes the Companion page. One-way (Companion doesn't stream OSC feedback) |
| `generic` | **Anything that listens to OSC** — QLab, Reaper, media servers, homemade rigs. The remap editor takes address templates with `{n}` as the strip number (fader/master/SELECT/MUTE/encoder each mappable, float 0-1 or int 0-100), and feedback arriving on the fader address moves the motors |

```
lx xtouch run                                     # grandMA3 onPC, this PC
lx xtouch run --target magicq                     # MagicQ, this PC
lx xtouch run --target x32 --host 192.168.1.30    # X32 on the network
lx xtouch run --target resolume
lx xtouch run --target companion                  # Companion, port 12321
```

Send ports default per target (MA3 8000, MA2 80 [websocket], MagicQ 8000,
Eos 8000, X32 10023,
Resolume 7000, Companion 12321, generic 9001). The X32 needs **no setup at all** — it
answers OSC out of the box; the bridge subscribes itself and keeps the
subscription alive.

## Surfaces (what's in your hands)

| Surface | What it does |
|---|---|
| **X-Touch (full size)** — default | The whole two-way conversation: motorised faders, scribble strips, LED rings, transport |
| **Akai MPK Mini** | The 8 **knobs** ride the target's encoder slots as absolute levels; the 8 **pads** press the SELECT row (Go / column-connect / channel-select per target) with pad-LED feedback. Defaults match the mk3 factory profile (knobs CC 70–77, pads notes 36–43) — both remappable in the config for any knobby MIDI box |
| **Behringer X32 / Midas M32** (DAW-remote MC mode) | The console itself as a control surface. Setup → Remote: encoder 1 enables remote control, encoder 2 selects **Mackie Control** (not HUI, not raw CC), then pick the interface: **Card MIDI** (easiest — it appears as an ordinary USB MIDI port), **MIDI In/Out** (DIN, needs a MIDI interface) or **RTPMIDI** (network; on Windows you need an rtpMIDI driver, and the port takes your session's name so pick it by hand). Card MIDI means the **expansion card's** USB-B socket — X-USB or X-LIVE, whichever is fitted. The X32 has *two* USB-B sockets on the back and the other one, **REMOTE**, is for X32-Edit and carries no MIDI at all: plugged into that one the PC sees no MIDI inputs whatsoever. That card's driver must be installed on the PC, and the MIDI port is then named after the **card**, e.g. `X-LIVE MIDI In 0` — not after the console. Then set REMOTE BUTTON to **ENABLED** and **press the DAW REMOTE button on the console's top panel** — the setup page only arms it; that button is what actually hands the **bus/output fader block** over to the DAW, which is why channels 1–8 do nothing. It is Mackie Control, so faders, SELECT/MUTE, encoders and the main fader all behave as the X-Touch does. Two differences: the X32 draws its own channel names so the scribble-strip SysEx is skipped, and it may not send fader-touch — in which case nothing is ever "held" and motor feedback simply applies |
| **Stream Deck** | Via **Bitfocus Companion**, which owns Stream Decks natively: add a *Generic OSC* connection in Companion aimed at the bridge's listen port (default 9000), and give buttons actions like `/xbridge/key/select/1` (value 1 on down, 0 on up), `/xbridge/fader/3` (0–100), `/xbridge/page` (bank number). Every deck key becomes a bridge control, and the same addresses work from TouchOSC or anything else that speaks OSC |

Pick the surface in the app panel or `--surface mpk` / config —
**or run several at once**: choose *X-Touch + MPK Mini* (config value
`"xtouch,mpk"`) and both are live simultaneously, each auto-detected by
port name, joining live if plugged in mid-session. Key state lights both
the X-Touch button and the matching MPK pad; motors stay X-Touch-only
(the MPK has none). The Stream Deck's OSC control port is always active
alongside whatever MIDI surfaces are connected — deck keys, faders and
pads all drive the same console at the same time. (The MA2 web-remote
session currently drives the primary surface only.) The OSC
control port speaks: `/xbridge/fader/<n>`, `/xbridge/master`,
`/xbridge/enc/<n>`, `/xbridge/key/select|mute/<n>`, `/xbridge/page`.

## Remapping (the nice way)

In the desktop app, section 7 → **Remap buttons & faders**: a grid of
every strip — fader/SELECT/MUTE/encoder per strip, master, page, prefix,
encoder sensitivity, and the transport-key commands — edit any of it and
**Save mapping**. The saved mapping is used on every bridge start (app and
CLI both). `lx xtouch config -o mymap.json` is the same thing as a file.

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

Menu → **In & Out → OSC**.

MA3's OSC menu has **one Port cell per line, used for both sending and
receiving** — the manual: *"the port configuration is used for sending and
receiving OSC data"*, with the hint that *"if you want to use different
ports for sending and receiving, you can create multiple configuration
lines"*. Identical in 2.1, 2.2 and 2.3. So if you went looking for a send
port and a receive port, you were looking for something that isn't there:
a round trip takes **two lines**.

The app writes both of them out with your actual ports filled in —
**Console setup…** in the panel, or:

```
xbridge ma3-setup                       # same PC, defaults
xbridge ma3-setup --bridge-ip 10.0.0.4  # bridge on another machine
```

First, at the top of the menu (neither is on by default):

| Toggle | Set to |
|---|---|
| Enable Input | **On** — nothing is received until it is |
| Enable Output | **On**, only if you want feedback |

**Line 1 — the console listens on 8000.** This is the one that makes
faders work.

| Cell | Value | Why |
|---|---|---|
| Mode | **UDP** | the bridge speaks UDP |
| Port | **8000** | must equal the bridge's send port |
| Prefix | **empty** | must match the bridge exactly; a mismatch is discarded in silence |
| Receive | **Yes** | not on by default, and separate from Enable Input |
| Receive Command | **Yes** | separate again — the master fader and transport keys use `/cmd` and are dead without it |
| Send | No | line 2 does the sending |
| Page / Fader / Key | defaults | the bridge speaks `/Page1/Fader201` |
| FaderRange | **100** | 255 makes every level read low |
| EchoInput | Yes while testing | shows arriving messages in the System Monitor |

**Line 2 — the console sends to 9000.** Only needed for feedback.

| Cell | Value | Why |
|---|---|---|
| Port | **9000** | must equal the bridge's listen port |
| Destination IP | the bridge's machine | `127.0.0.1` on one PC |
| Send | **Yes** | this is the sending line |
| Receive | **No** | with Receive on, MA3 also *binds* 9000 — on one PC it fights the bridge for it |
| EchoOutput | Yes while testing | shows what the console emits |

The quickest proof that line 1 is right: turn on EchoInput, open **Add
Window → More → System Monitor**, and move a fader. If nothing appears
there, the message is not reaching MA3 at all and no bridge setting will
help — it's Enable Input, Receive, the port, or the IP.

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

## Running from the app instead of the terminal

The desktop app has the bridge as **section 7**: set the MA3 host (leave
`127.0.0.1` on the same PC), hit **Start bridge**, and the panel shows
live state — which MIDI port it found, how many messages each side has
sent. Start/stop it there; no terminal needed.

## Proving a surface with the MIDI monitor

While the bridge is running the panel shows **surface says:** — the last
few messages the surface sent, as raw bytes *and* as what the bridge made
of them:

```
xtouch  e0 00 60  fader 1 -> 75%
xtouch  90 18 7f  SELECT 1 down
xtouch  b0 10 01  encoder 1 +1
x32mc   b0 63 07  (unmapped)
```

This separates two failures that look identical from the console end:

- **A control produces no line at all.** It never reached the bridge —
  wrong MIDI port, wrong console mode, or the control isn't sent in that
  mode. No mapping change will help.
- **A control produces a line reading `(unmapped)`.** It arrived; the
  bridge just doesn't know it yet. The hex is there so it can be told
  what to do with it.

An X32 in MC mode is a good rig for this even if the desk it drives is a
different one: press each control and watch what the console actually
emits.

## If something doesn't line up: the sniffer

```
lx xtouch sniff
```

prints **everything both sides say, decoded** — every OSC message MA3
sends (exact address and value) and every MIDI event from the surface —
for 30 seconds. If your MA3 version speaks a slightly different OSC
dialect, one fader wiggle in MA3 shows the exact address it uses, and the
config can be set to match. Run it while the bridge is *stopped* (they
share the listen port).

## Reliability

- Start the bridge before the X-Touch is plugged in — it waits and
  connects when the surface appears.
- Unplug the X-Touch mid-session — it reconnects automatically.
- Restart MA3 — nothing to do; OSC is connectionless, it just resumes.

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

## Real MA3 names on the scribble strips

Import `data/ma3-plugin/lxtool_labels.lua` into MA3 (Show Creator →
Import → Plugin) and run it: it pushes the executors' actual names to the
strips every few seconds over the same OSC line. Experimental — written
against the MA3 v2.x Lua API, not yet run on a console; the file says
what to tweak if names don't appear.

## Screenshots straight in (OCR)

The chart box and the patch-sheet box each take an image: the app reads
it with the OS's own OCR (Vision on macOS, Windows OCR on Windows — no
internet, nothing extra to install with the packaged app). Check the
lifted text, then hit the read button as usual. On other platforms the
phone select-text-in-image route still works.

## Sharing mappings

**Export preset** in the remap editor downloads the current mapping as a
JSON file; the import chooser applies one. Send a mate your mapping,
they import it, done. The same file works with
`lx xtouch run --config <file>`.

## Installer signing status

macOS builds are now ad-hoc signed in CI (prevents the "damaged app"
refusal). Full no-warning installs need paid certificates — an Apple
Developer ID (~USD 99/yr, plus notarisation) and a Windows Authenticode
certificate — which only the project owner can purchase; the CI has a
marked slot ready for the day the secrets exist. Until then:
right-click → Open on macOS, "More info → Run anyway" on Windows.

## If Windows Defender flags the download

Defender sometimes quarantines the unsigned `.exe` as
**Trojan:Win32/Wacatac.B!ml**. The `!ml` suffix means a machine-learning
heuristic, not a match against known malware — unsigned single-file
Python apps trip it routinely, and this build comes from the repository's
own public CI with its SHA-256 recorded on the release page.

To verify and unblock:

1. Check the hash matches the release: in PowerShell,
   `Get-FileHash .\XBridge.exe` and compare with the digest shown on
   https://github.com/Wntr2134/LX-Tool/releases (expand the asset).
2. Windows Security → Protection history → the XBridge entry →
   **Actions → Restore**, then **Allow on device**.
3. Optional: report the false positive to Microsoft at
   https://www.microsoft.com/wdsi/filesubmission — a few reports usually
   clears the heuristic for everyone.

The builds carry proper Windows version metadata to lower the heuristic
score; the real fix is code signing, which needs a purchased certificate
(see the signing section).

## "unknown port" / the surface will not connect

Click **MIDI ports** in the panel (or run `xbridge ports`) to see exactly
what this machine can see. Two things that bite on Windows:

- **The device is named differently in each direction** - `X-Touch 0` on
  the input side, `X-Touch 1` on the output side, for one physical unit.
  The bridge pairs them up itself; if you override with `--midi-port`,
  give it the **input** name and the output is matched automatically.
- **Windows MIDI ports are exclusive-access.** If a DAW, another copy of
  XBridge, or Behringer's own editor holds the X-Touch, nothing else can
  open it. Close them and hit Start again.

If the panel says *"no surface on USB"*, it lists every MIDI input it
did see - if the X-Touch is not among them, it is not in **MC mode**
(hold channel-1 SELECT while powering on: encoder 1 = MC, encoder 2 =
USB), or it is on a USB hub that is dropping it.

## The faders move but the console does not

The surface connecting (LEDs light, strips show text) only proves the
**MIDI** half. Use the panel to see the other half:

- **The counters** now read `MIDI in / sent to console / console in`.
  If *MIDI in* climbs when you move a fader but *sent to console* does
  not, the surface event is not being mapped - tell me what you moved.
  If *sent* climbs and nothing happens on the desk, the OSC is leaving
  this machine and the problem is address, port or the console's own
  setup.
- **last sent** shows the actual messages, e.g. `/Page1/Fader201 50`.
- **Test fader 1** sends one of those without touching the hardware, so
  the console can be proved on its own.
- **Find MA3 format** is the shortcut when *sent* climbs and the desk
  ignores it. MA3 processes only messages that begin with the prefix on
  its OSC page, and the "Page"/"Fader" address cells on that line are
  editable too - any mismatch is discarded in silence, which looks
  exactly like a dead bridge. The sweep walks twelve combinations of
  prefix (none / `gma3`), value type (int 0-100, float 0-100, float 0-1,
  int 0-255) and address shape (`/Page1/Fader201` or `/Fader201`), 1.5s
  apart. Watch your executor: whichever step moves it is your format, and
  **Keep** writes it into the mapping. From a terminal:
  `xbridge probe --exec 201`.

Then check, in this order:

1. **Does anything live at that executor?** `/Page1/Fader201` moves
   executor **201 on page 1** - if your show has nothing assigned there,
   a perfectly delivered message does nothing. Assign a sequence to
   executor 201, or change the mapping to the executors you actually
   use (Remap → Fader→exec).
2. **Ports are crossed over.** The bridge *sends* to MA3's **input**
   port (8000 by default) and *listens* on 9000. In MA3's OSC line those
   are the other way round from the bridge's point of view - the
   console's "output/destination" port must be the bridge's listen port.
3. **Destination IP.** On the same PC use `127.0.0.1`. MA3 must be
   sending to, and receiving on, an interface that actually carries it.
4. **Console-side switches.** All of these fail silently and look
   identical from the bridge: **Enable Input** off; **Receive** not set
   to Yes on the OSC line (it is not on by default); **Receive Command**
   not set to Yes, which alone kills the master fader and transport keys
   because those use `/cmd`.
5. **Prefix, value type and address shape.** These ship matching a stock
   console - no prefix, int 0-100, `/Page1/Fader201`, which is the
   manual's own example. MA's Open Stage Control walkthrough instead
   assumes you have set the prefix to `gma3`. If your OSC page differs,
   don't guess: run **Find MA3 format**.
6. **Feedback needs its own OSC line.** One line uses the same port to
   send and receive, so a single line on 8000 cannot also send to the
   bridge's listen port. Add a second line for the return leg.
7. **The command line is a second route — and it works.** Confirmed on
   grandMA3 2.4.2: `FaderMaster 201 At 50` typed into the console's
   command line moves executor 201. Note the shape — MA's *OSC* page
   prints `FaderMaster Page 1.201 At 50`, and 2.4 answers that with
   **IllegalProperty**, because the FaderMaster *keyword* page documents
   `FaderMaster [Object] [Number] At [Value]` and `Page` is not an object
   type in that slot. The two manual pages disagree; the keyword page is
   the one that runs. The last two sweep steps
   The last sweep steps send `/cmd` with that syntax instead of
   addressing the executor. That path ignores the OSC line's Fader and
   Page address cells entirely and needs **Receive Command** rather than
   Receive. If only those steps move the fader, executor addressing is
   the problem — and **Keep** on one is a perfectly good way to run the
   show, not just a diagnosis. One catch: the bare form acts on the
   console's **current page**, so the surface's bank buttons will not
   change which executors are hit. Set `ma3_page_cmd` (try
   `Select Page {page}`) if the console should follow the bank.
8. **Motor faders need a feedback map.** MA3 does not echo
   `/Page1/Fader201` back; playback feedback arrives addressed by pool
   index, e.g. `/13.13.1.6.1,sif,"FaderMaster",3,63.5`. Only you know
   which object is on which strip, so set `ma3_feedback` in the mapping:
   `{"13.13.1.6.1": 1}` drives strip 1. Run `xbridge sniff` with Send and
   EchoOutput enabled on the console to see the addresses to use.
