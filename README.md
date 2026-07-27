# LX-Tool

Fixture library tooling for lighting techs. Point it at a new fixture from a
global database and it tells you whether your ChamSys library already has it,
what the closest existing head is, and exactly which channels you'd have to
change — in the order you should change them. It also converts fixtures
between GDTF, Open Fixture Library and grandMA2.

## Why

A new fixture turns up. It's on GDTF Share or Open Fixture Library but not in
your MagicQ library. Today that means eyeballing channel lists and building a
head by hand. This does the comparison for you.

## Download

The easiest way in is the desktop app - no Python, no terminal. Grab it from
[Releases](https://github.com/Wntr2134/LX-Tool/releases):

| Platform | File |
|---|---|
| macOS | `LX-Tool-macos.zip` - unzip, drag to Applications |
| Windows | `LX-Tool.exe` - double-click |

It opens as a normal application window - its own dock/taskbar icon, no
browser, no address bar - drawn by the platform's own webview (WKWebView on
macOS, WebView2 on Windows). If neither is available it falls back to opening
your default browser rather than failing.

It runs entirely on your machine: it reads your MagicQ heads folder, and
nothing about your library leaves the computer. On first launch it finds your
heads folder automatically.

**macOS will say "unidentified developer"** - the builds are unsigned. Right-click
the app, choose Open, then Open again. Once only. **Windows SmartScreen** shows
"More info" -> "Run anyway" for the same reason.

## Install from source

Needs Python 3.9 or newer (tested on 3.11). From the repo root:

```bash
git clone https://github.com/Wntr2134/LX-Tool.git
cd LX-Tool
git checkout claude/fixture-library-converter-73139h

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install --upgrade pip          # editable installs need pip >= 21.3
pip install -e ".[web]"
```

macOS ships an old pip that predates editable installs from `pyproject.toml`.
If you see *"File setup.py or setup.cfg not found"*, upgrade pip as above, or
install non-editable with `pip install ".[web]"`.

That installs an `lx` command you can run from any directory. The library and
CLI have **no third-party dependencies**; only the web UI pulls in FastAPI, so
`pip install -e .` alone is enough for CLI-only use.

Check it worked:

```bash
lx --help
```

## Use

### Web UI

```bash
lx-web            # or: uvicorn lxtool.web.app:app --reload
```

Then open <http://127.0.0.1:8000>. Paste your MagicQ `heads` folder path,
scan it, and search or upload a fixture.

Your heads folder is usually:

MagicQ nests it under its show directory, as `<MagicQ folder>/show/heads`:

| OS | Path |
|---|---|
| macOS | `~/Documents/MagicQ/show/heads` |
| Windows | `C:\MagicQ\show\heads` (or under `ProgramData`) |
| Linux | `~/MagicQ/show/heads` |

If none of those exist, search for the files themselves:

```bash
find ~ /Applications -name "*.hed" 2>/dev/null | head -5    # macOS/Linux
dir /s /b C:\*.hed                                          # Windows
```

### CLI

```bash
# Pull the Open Fixture Library once (~3 MB), then work offline
lx fetch
lx search "mac 700"
lx search "led par rgbw" --channels 8

# Index your ChamSys library
lx scan ~/Documents/MagicQ/heads -v

# Does my library already have this? What would I change?
lx match new_fixture.gdtf ~/Documents/MagicQ/heads

# ...or skip the file entirely and match straight from the catalogue
lx match --ofl "mac 700" ~/Documents/MagicQ/heads

# Convert between formats
lx convert fixture.json fixture.gdtf     # OFL  -> GDTF (import into MagicQ)
lx convert fixture.json fixture.hed      # OFL  -> ChamSys head (experimental)
lx convert fixture.gdtf fixture.xml      # GDTF -> MA2
lx convert ma2_export.xml fixture.gdtf   # MA2  -> GDTF

# Inspect a file, including decoding a .hed
lx doctor some_head.hed -v
```

Example `match` output:

```
Target: Robe Robin 600 [Mode 1] - 16 ch

 1. [  78%] Robe Robin 300 [16ch]
        - manufacturer matches
        - same footprint (16 ch)
        - 3 change(s) needed, worst severity 4/5

To use 'Robe Robin 300 [16ch]', change (most critical first):

   ch   5  retype     Tilt           library has Zoom (Zoom), needs Tilt
   ch   9  move       ColorWheel     move from ch 9 to ch 11
   ch  14  add        Frost          add Frost (Frost)
```

Changes are ordered by how badly they break the fixture — intensity and
position first, then colour, then beam, then control — and within that by
patch order, so you walk the channel list once.

## How matching works

Three things are scored separately, because they mean different things to
whoever has to fix the rig:

- **Content** — do the right parameters exist at all? (Jaccard over attributes.)
  Missing channels mean building a head.
- **Ordering** — are they in the right slots? (Sequence alignment.) Right
  channels in the wrong order is a repatch, not a rebuild.
- **Colour system** — CMY, RGB, RGBW, hybrid or wheel. A CMY head is not a
  substitute for an RGB one however similar the names look, so a mismatch here
  is heavily penalised.

Comparison uses **sequence alignment**, not slot-by-slot equality. That matters:
if a fixture inserts one channel near the top, every later offset shifts. A
positional comparison would call all of them wrong; alignment reports the one
insertion and leaves the rest matched.

### Offline by design

`lx fetch` caches the whole Open Fixture Library locally (`~/.cache/lxtool`,
override with `LXTOOL_CACHE`). Search and match then work with no connection —
which is where you actually need them.

### GDTF export is tuned for import

Exported GDTF uses the **standard GDTF attribute names** (`Shutter1`,
`ColorAdd_R`, `Gobo1Pos`, `Color1`…) with correct `FeatureGroup.Feature`
assignments, `Geometry_Attribute` channel names, and `Snap="Yes"` on wheel
parameters. That is what makes MagicQ and MA3 land each channel on the right
encoder instead of dumping it into a generic slot.

## Format support

| Format | Read | Write | Notes |
|---|:--:|:--:|---|
| GDTF (`.gdtf`) | yes | yes | The interchange format. MA3, MagicQ and others import it |
| MVR (`.mvr`) | yes | yes | Whole rigs: fixtures, modes, addresses, layers. **Unvalidated** — see below |
| Open Fixture Library (`.json`) | yes | — | Open API, ~620 fixtures cached offline |
| grandMA3 XML (`.xml`) | yes | via GDTF | Reads MA3's `lib_fixture_types/grandma3` library directly |
| grandMA2 XML (`.xml`) | yes | yes | **Not yet validated against a real MA2 export** — see below |
| grandMA2 `.pxml` | no | no | MA2's local library is encrypted — see below |
| ChamSys (`.hed`) | yes | experimental | Obfuscation solved — see [docs/hed-format.md](docs/hed-format.md) |

### grandMA3

MA3 is **GDTF-native** for fixture types and **MVR** for the patch, so both
directions are already covered by the table above — there is no separate MA3
format to add. Export a fixture type from MA3 as GDTF and read it directly;
export a whole rig as MVR and use `lx rig`.

```bash
lx rig show.mvr        # patch summary per universe, with address-clash detection
lx doctor show.mvr     # what the archive carries and what it's missing

# MA3's own fixture library reads directly
lx doctor ~/MALightingTechnology/gma3_*/shared/lib_fixture_types/grandma3/"ayrton@alienpix-rs.xml" -v
```

MA3 fixture XML is GDTF-derived but not GDTF: DMX slots are `Coarse`/`Fine`
attributes rather than a single `Offset`, DMX values are 24-bit hex triplets,
and attributes use MA3 spellings like `ColorRGB_R`. `lxtool/formats/ma3.py`
handles those; files are detected by content, so `.xml` routes itself to the
MA3, GDTF or MA2 reader automatically.

One gap: `GeometryReference` repetition (multi-element fixtures) is not
expanded, so referenced channels are counted once. MA3 writes the true count
into the mode name — `"Ex 16 Bit (52 ch)"` — and that is recorded as the
declared footprint, so the size stays correct even where the channel list is
partial.

`lx rig` flags overlapping DMX footprints, which is the failure mode when a
patch moves between desks: a mode that is 8 channels in one library can be 10
in another, and the rig silently collides.

### ChamSys

Head files are read directly, so `lx scan` gives real channel lists and
matches are channel-by-channel:

```
$ lx match new_par.json ~/Documents/MagicQ/heads
 1. [  70%] China 7x9WMiniParRGB [7ch]
        - same footprint (7 ch)
        - same colour system (RGBW)
        - 2 change(s) needed, worst severity 1/5

To use 'China 7x9WMiniParRGB [7ch]', change (most critical first):

   ch   6  remove     Speed          remove Speed (Speed)
   ch   7  add        Macro          insert Macro (Macro)
```

For getting a fixture *into* MagicQ, two routes: `lx convert x.json out.gdtf`
and import it (no unknowns), or `lx convert x.json out.hed` and drop it in the
heads folder (experimental — verify in the Head Editor).

### Known limitations, stated plainly

**ChamSys `.hed` writing is experimental.** *Reading* is solved and exact —
the obfuscation is XOR against a down-counter mod 127, documented in
[docs/hed-format.md](docs/hed-format.md) — so library scans and matches now
work channel by channel. Writing emits a correct header and channel block,
but a `.hed` also carries trailing sections (palettes, ranges, per-channel
defaults) that have not been fully decoded. Check any generated `.hed` in the
Head Editor; GDTF import is still the route with no unknowns in it.

**MVR is unvalidated too.** Written against the published structure, parsed
namespace-agnostically, and it degrades safely: a fixture whose GDTF isn't
embedded still comes through with its address, mode and layer rather than
vanishing from the patch. Both address conventions (continuous, and
break-relative) are handled. Run `lx doctor <file>.mvr` on a real MA3 export
and check the counts before trusting it.

**grandMA2's local library (`.pxml`) is encrypted and unsupported.** Measured
on a real file: 7.978 bits/byte entropy, all 256 byte values, no container
magic, no decompressor succeeds at any offset, and no repeated 16-byte blocks.
That is encryption, not obfuscation, and unlike the ChamSys scheme there is no
handle to attack. Export fixture types from the desk as XML instead, or use
MA3/GDTF.

**The grandMA2 reader is unvalidated.** MA2's schema isn't published the way
GDTF's is, so the parser is written tolerantly and anything it can't interpret
becomes `Unknown` rather than a confident wrong guess. Run
`python -m lxtool.cli doctor <file> -v` on a real MA2 export and check the
unmapped count before trusting it. For **MA3, use GDTF** — MA3 is GDTF-native,
so that path is exact.

## Development

```bash
pip install -e ".[web,dev]"
pytest -q
```

## Layout

```
lxtool/
  model.py        canonical Fixture / Mode / Channel / Range
  attributes.py   cross-library attribute vocabulary and normalisation
  matching.py     scoring and the ordered change plan
  cli.py          the `lx` commands
  formats/
    gdtf.py       GDTF read + write
    ofl.py        Open Fixture Library read
    ma2.py        grandMA2 XML read + write
    chamsys.py    library scanning, index CSVs, .hed detection
  web/app.py      FastAPI local UI
docs/hed-format.md
```

Every format is parsed into the one canonical model in `model.py`, so adding
format N+1 costs a reader and a writer rather than N converters.
