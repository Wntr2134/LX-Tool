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
thing in section 6, with the editor built in.

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
expands "5-6 Zoom (16 bit)" into a proper 16-bit pair, handles phone OCR
that reads a table column-by-column, and attaches range rows to the right
channel. In the app: paste into the chart box in section 6.

**"What is this clone, really?"** If you've drafted a plan or pasted a chart
and want to know which known fixture it's closest to (often the genuine
profile to start from), the app's **What is this?** button ranks the whole
library against your layout. On the command line, add `--match`:

```bash
lx head from-text chart.txt --match
#   Closest known fixtures - consider starting from one of these:
#     1. [ 92%] Martin Aura [Standard]  <ChamSys>
```

**Nothing but a patch sheet?** The touring worst case: the sheet says
"22C Moving Head, 22 Ch", the brand is an OEM nobody has heard of, and no
manual exists anywhere. Draft the typical clone layout for that fixture
type — cheap fixtures are deeply conventional, so the draft lands roughly
right — and verify it at load-in:

```bash
lx head template draft.plan --stock beam --channels 16
# kinds: beam, spot, wash, par, strobe, spark
lx head build draft.plan
```

In the app: the "Draft a typical clone layout" row in section 6. Every
draft opens with the **ten-minute fader test**: patch one unit, dimmer to
full, sweep each channel 0–255 and note what actually happens, rename or
reorder the rows to match, rebuild. Even faster on the day: every one of
these fixtures shows its channel list in its own menu display — photograph
that and use the chart box instead. The draft is a starting point, not a
promise; the fader test is what makes it true.

**No reference at all?** Start blank and name channels as you probe the
fixture with a DMX tester:

```bash
lx head template plan.txt --blank 16
```

**In the app**, section 6 is a full editor: **drag the grip** on each
channel to reorder the DMX layout, edit names and attributes inline, add or
remove channels, then Check and Build. "Raw text" toggles to the plain-text
plan if you prefer typing.

**Save the ones you crack.** A clone you figure out at one venue should be
there at the next. Build it with `--save` (or the app's **Save to my heads**
button) and it joins your personal library:

```bash
lx head build plan.txt --save
lx heads list                     # everything you have saved
lx heads edit China_AuraClone_14ch -o plan.txt   # reopen to tweak
```

Saved heads live under your user folder (override with `LXTOOL_MYHEADS`) and
**automatically join every match and "What is this?" from then on** - so the
second time you meet that clone, the tool recognises it. The app has them in
section 7, with Edit / Download / Remove.

## Reading a whole patch sheet

Paste the patch sheet you were sent — a spreadsheet export, or the text your
phone lifts off a screenshot (select-text-in-image) — into **section 5**, or:

```bash
lx sheet sheet.txt        # or:  pbpaste | lx sheet -
```

It groups the rows into fixture types (how many, which universes, what
channel count), checks each against your saved heads and the catalogue,
**flags DMX address collisions** on the sheet itself, and gives every
unknown fixture a one-click "Draft head" that opens the head builder with
the right layout and channel count already filled in.

## Bulk conversion (ChamSys → MA3 and more)

MA3 imports GDTF natively, so a whole ChamSys show's heads can move across
in one go. In the app: select several files in section 4 (it takes `.hed`
too) and you get one zip back. Command line:

```bash
lx convert heads/ ma3-out/ --to gdtf
```

Every readable fixture file in the folder is converted; unreadable ones are
skipped and named, never silently dropped. Drag the resulting `.gdtf` files
onto the MA3, or copy them to a USB stick's `gma3_library` folder.

## XBridge (separate app)

The X-Touch control-surface bridge is its own app now: **XBridge**, in the
`xbridge/` folder with its own download (`XBridge.exe` /
`XBridge-macos.zip` on the same releases page). See `xbridge/README.md`.

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
