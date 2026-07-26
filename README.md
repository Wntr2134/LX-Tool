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

## Install

```bash
pip install -r requirements.txt
```

Python 3.11+. The library and CLI have **no third-party dependencies** — only
the web UI needs FastAPI.

## Use

### Web UI

```bash
uvicorn lxtool.web.app:app --reload
```

Open <http://127.0.0.1:8000>, point it at your MagicQ `heads` folder, and
upload a fixture file.

### CLI

```bash
# Pull the Open Fixture Library once (~3 MB), then work offline
python -m lxtool.cli fetch
python -m lxtool.cli search "mac 700"
python -m lxtool.cli search "led par rgbw" --channels 8

# Index your ChamSys library
python -m lxtool.cli scan ~/Documents/MagicQ/heads -v

# Does my library already have this? What would I change?
python -m lxtool.cli match new_fixture.gdtf ~/Documents/MagicQ/heads

# ...or skip the file entirely and match straight from the catalogue
python -m lxtool.cli match --ofl "mac 700" ~/Documents/MagicQ/heads

# Convert between formats
python -m lxtool.cli convert fixture.json fixture.gdtf     # OFL  -> GDTF
python -m lxtool.cli convert fixture.gdtf fixture.xml      # GDTF -> MA2
python -m lxtool.cli convert ma2_export.xml fixture.gdtf   # MA2  -> GDTF

# What can you actually read from this file?
python -m lxtool.cli doctor some_head.hed
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
| grandMA2 XML (`.xml`) | yes | yes | **Not yet validated against a real MA2 export** — see below |
| ChamSys (`.hed`) | filename + CSV index | via GDTF | Bodies are obfuscated — see [docs/hed-format.md](docs/hed-format.md) |

### grandMA3

MA3 is **GDTF-native** for fixture types and **MVR** for the patch, so both
directions are already covered by the table above — there is no separate MA3
format to add. Export a fixture type from MA3 as GDTF and read it directly;
export a whole rig as MVR and use `lx rig`.

```bash
lx rig show.mvr        # patch summary per universe, with address-clash detection
lx doctor show.mvr     # what the archive carries and what it's missing
```

`lx rig` flags overlapping DMX footprints, which is the failure mode when a
patch moves between desks: a mode that is 8 channels in one library can be 10
in another, and the rig silently collides.

### Getting fixtures into ChamSys

MagicQ imports GDTF natively, so the supported route is
`anything -> GDTF -> MagicQ import`. LX-Tool never needs to write `.hed`.

### Two known limitations, stated plainly

**ChamSys `.hed` bodies are obfuscated.** LX-Tool indexes a ChamSys library
from head *filenames* (`Manufacturer_Model_Mode.hed`) and the plain-text
`headmapcapture.csv` / `manufacturer_exceptions.csv`. That's enough to answer
"do I have this fixture, in what modes, at what channel count" — but not to
diff channel-by-channel against your existing heads. Matches against ChamSys
are therefore scored on footprint and name only, capped below "exact", and
labelled as such. They are never presented as more certain than they are.
[docs/hed-format.md](docs/hed-format.md) records the full analysis and the one
step that would finish it.

**MVR is unvalidated too.** Written against the published structure, parsed
namespace-agnostically, and it degrades safely: a fixture whose GDTF isn't
embedded still comes through with its address, mode and layer rather than
vanishing from the patch. Both address conventions (continuous, and
break-relative) are handled. Run `lx doctor <file>.mvr` on a real MA3 export
and check the counts before trusting it.

**The grandMA2 reader is unvalidated.** MA2's schema isn't published the way
GDTF's is, so the parser is written tolerantly and anything it can't interpret
becomes `Unknown` rather than a confident wrong guess. Run
`python -m lxtool.cli doctor <file> -v` on a real MA2 export and check the
unmapped count before trusting it. For **MA3, use GDTF** — MA3 is GDTF-native,
so that path is exact.

## Development

```bash
python -m pytest tests/ -q
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
