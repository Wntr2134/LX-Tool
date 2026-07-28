# LX-Tool — getting started (for testers)

**What this is:** you turn up at a venue, the desk's library doesn't have the
fixture you've been handed, and you'd normally spend an hour building a head
by hand from the manual. LX-Tool builds it for you: it searches a library of
~17,000 fixtures, and writes a ready-to-patch **ChamSys MagicQ head file**
(`.hed`) — with the right channels, 16-bit pairs, defaults, colour swatches
and range names. Generated heads pass every check in MagicQ's own Head
Editor. It also converts GDTF and grandMA files, and can tell you the
*closest* head you already have and what to change on it.

You need internet **once** to download the fixture catalogue. After that it
works offline — fine for venues with no signal.

---

## Install

### Option A — desktop app (easiest)

1. Download the app for your machine from
   **https://github.com/Wntr2134/LX-Tool/releases** (Mac: `LX-Tool-macos.zip`,
   Windows: `LX-Tool.exe`).
2. **Mac:** unzip, then **right-click the app → Open → Open** the first time
   (it isn't signed with an Apple certificate, so double-clicking will be
   blocked with an "unidentified developer" message — right-click → Open is
   the way around it).
   **Windows:** if SmartScreen objects, click **More info → Run anyway**.
3. The app opens a window with the same functions as the commands below —
   Download catalogue, Search, Convert, Scan, Match.

### Option B — command line (Mac/Linux/Windows with Python 3.9+)

```bash
git clone https://github.com/Wntr2134/LX-Tool.git
cd LX-Tool
python3 -m pip install -e ".[web]"
lx --help
```

---

## The five-minute walkthrough (the whole point of the app)

Scenario: you're on a MagicQ desk and someone hands you a **Martin MAC Aura**
the desk doesn't have.

**1. Get the catalogue** (once, needs internet):

```bash
lx fetch
```

**2. Find the fixture:**

```bash
lx search "mac aura"
#  martin/mac-aura
#      Martin MAC Aura  [Moving Head, Color Changer]
#      Standard (14ch), Extended (25ch)
```

**3. Build the head.** The filename matters — MagicQ reads
`Manufacturer_Model_Mode.hed`:

```bash
lx convert --ofl martin/mac-aura --mode Standard Martin_MACAura_Standard.hed
```

**4. Get it onto the desk.**
- **MagicQ PC (Mac):** copy the file into `~/Documents/MagicQ/show/heads/`
- **MagicQ PC (Windows):** `C:\MagicQ\show\heads\`
- **Console:** copy to USB, then transfer into the heads folder with the
  console's file manager.

Then **restart MagicQ** (it reads the heads folder at startup).

**5. Patch it:** Setup → Patch → Choose Head → Martin → MAC Aura → patch.
Pan/tilt home centred, colours carry their gel swatches, shutter and zoom
ranges are named and typed.

Also works from a GDTF file if you have one instead:

```bash
lx convert somefixture.gdtf Manufacturer_Model_Mode.hed
```

---

## Making your own head (clones and manuals)

Two situations the catalogue can't help with, and what to do:

**The venue clone.** The fixture is badged as a known model but it's a copy
with the channels in a different order — the genuine profile patches, then
pan sits on the colour wheel. Start from the genuine profile and rearrange:

```bash
lx head template plan.txt --ofl martin/mac-aura --mode Standard
# open plan.txt in any text editor: reorder the channel lines,
# change "manufacturer:" to China (or whoever), fix what's wrong
lx head build plan.txt
```

The plan is one line per DMX channel — moving a line moves the channel, and
the gobo/colour ranges under it move too. The desktop app has the same
thing in section 5, with the editor built in.

**The manual-only fixture.** No profile anywhere, just a DMX chart in the
manual. Photograph the chart, then use your phone or Mac's select-text-in-
image (built into iOS/macOS Photos and Preview) to copy the table as text,
and:

```bash
lx head from-text chart.txt          # writes head-plan.txt as a draft
# CHECK the draft against the manual, fix anything misread
lx head build head-plan.txt
```

It copes with messy manual layouts ("CH1 - Pan", "3. Tilt", "0-9 Open"),
expands "5-6 Zoom (16 bit)" into a proper 16-bit pair, and attaches range
rows to the right channel. In the app: paste into the chart box in
section 5.

## Two other tricks worth trying

**"What's the closest head I already have?"** — point it at a fixture file
and it ranks your existing MagicQ library by similarity and lists exactly
which channels differ, in order:

```bash
lx match somefixture.gdtf
```

**"What's in this rig file?"** — reads a whole MVR rig export, lists the
patch, flags address clashes:

```bash
lx rig show.mvr
```

---

## What to test and report

The most useful reports, roughly in order:

1. **Did install work on your machine?** OS + what happened.
2. Convert a fixture *you know well* and patch it. Do the channels do what
   the label says? Is 16-bit pan/tilt smooth? Do colours home sensibly?
3. Open the generated head in MagicQ's Head Editor — the title bar should
   show **no ERRORS**.
4. Anything that crashes, errors, or looks wrong: send the exact command or
   button, a screenshot, and (if a head is involved) the `.hed` file itself.

**Known limits, honestly stated:**
- The fixture must exist in the Open Fixture Library, or you need its GDTF.
- The Windows build has had less testing than the Mac one — Windows reports
  are extra valuable.
- The apps are unsigned (no Apple/Microsoft certificate), hence the
  first-open warnings above.
- MagicQ caches heads inside show files: if you replace a `.hed` you've
  already patched, unpatch it, restart, and re-patch to pick up the change.
- ChamSys `.pxml` profile files are encrypted and not supported; `.hed` is
  the format this tool speaks.

Report problems at https://github.com/Wntr2134/LX-Tool/issues (or just
message me).
