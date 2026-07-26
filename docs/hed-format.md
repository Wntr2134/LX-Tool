# ChamSys `.hed` head files — format analysis

## Status

**Head file *bodies* cannot currently be decoded.** Everything LX-Tool does with a
ChamSys library works from filenames and the plain-text index CSVs instead.
This document records what was established about the encoding so the work
isn't repeated, and sets out the one concrete step that would finish it.

## What was tested

Analysis was run against real head files from a working MagicQ `heads` folder
(`China_7x9WMiniParRGB_7ch.hed`, `Laserworld_EL-400RGB_9ch.hed`,
`SPOT MOVERS.hed`, `test_test_SPOT MOVERS.hed`).

### Established facts

1. **Bodies are not plain text.** Every non-newline byte lies in `0x80`–`0xFF`.
2. **Exactly 128 distinct byte values occur**, so each byte carries 7 bits of
   payload with the high bit always set.
3. **Newlines survive as literal `0x0A`.** They are the only bytes below
   `0x80`, which means line structure is preserved and lines are encoded
   individually or the newline is skipped by the encoder.
4. **The keystream is position-locked, not chained.** Two head files saved
   from the same source personality under different names
   (`SPOT MOVERS.hed` vs `test_test_SPOT MOVERS.hed`) differ in only 11 bytes
   out of 1529, and those differences do **not** cascade — byte 127 onward is
   identical after a difference at byte 126. A chained/CBC-style cipher would
   have diverged permanently from the first difference.
5. **A fixed header exists.** The first 72-byte line is byte-identical across
   fixtures from completely different manufacturers.

### Ruled out

| Hypothesis | How it was tested | Result |
|---|---|---|
| Plain 7-bit ASCII with high bit set (`b & 0x7F`) | Direct decode | Garbage. `HED` appears at offset 25 but is coincidental |
| Single-byte XOR / add / subtract | All 256 keys × 3 ops, scored on printability | Best 0.88 printable, no readable text |
| Complement (`0xFF - b`) | Direct decode | Garbage |
| Monoalphabetic substitution | Byte-frequency analysis | Frequency is flat (max 1.2% vs ~15% expected for space in text) — ruled out |
| Affine substitution `(m·b + k) mod 128` | All 64 odd multipliers × 128 offsets, scored on lighting vocabulary | No candidate produced a single dictionary hit |
| Position-linear keystream `(b ± i + k)` | Per-line and global index, all offsets, both signs | Garbage |
| Full affine-in-position `(b + a·i + k)` | 128 × 128 × 2 index modes | Garbage |
| Repeating key (Vigenère) | Index of coincidence, periods 1–48 | IC flat at 0.0076 ≈ random (1/128 = 0.0078) — **no repeating key up to length 48** |

### Conclusion

The encoding uses a **long, non-repeating, position-locked keystream** over
7-bit values — consistent with a PRNG or LFSR seeded identically for every
file. That is not recoverable by statistical analysis of ciphertext alone in
reasonable time.

## How to finish this

Because the keystream is **fixed and position-locked**, a *single*
known-plaintext/ciphertext pair recovers it for every byte position that pair
covers — and therefore unlocks that prefix of *every* head file in the
library. A large head file (6 KB+) would cover essentially the whole library.

Concretely, one of these would do it:

1. **A plain-text head file plus its saved `.hed`.** Older MagicQ versions and
   hand-authored heads are plain text. If a plain-text head can be loaded into
   MagicQ's Head Editor and re-saved, the before/after pair yields the
   keystream directly.
2. **A deliberately trivial head.** Create a head in the Head Editor whose
   channel names are a known repeating string (e.g. twenty channels all named
   `AAAAAAAA`), save it, and supply the file. The known plaintext gives the
   keystream by subtraction/XOR at each position.

Once recovered, drop the keystream into
`lxtool/formats/chamsys.py::decode_hed`. The rest of the toolchain already
routes through that function, so channel-level comparison against the ChamSys
library would start working with no other changes.

## Why this isn't blocking

MagicQ **imports GDTF natively** (added in v1.9.0, improved through v1.9.8.3).
So the highest-value direction — getting a new fixture *into* ChamSys — never
needed `.hed` writing at all: LX-Tool emits GDTF and MagicQ imports it.

Decoding `.hed` would improve one specific thing: comparing a new fixture
against your existing library *channel by channel* rather than by
footprint and name. Until then, matching against ChamSys is explicitly capped
below "exact" and labelled `channel detail unavailable — compared on
footprint only`, so the output never overstates its confidence.

## Plain-text files in the `heads` folder

These are readable and are what the scanner actually uses:

| File | Contents |
|---|---|
| `<Manufacturer>_<Model>_<Mode>.hed` | Filename encodes manufacturer, model and mode; mode usually carries the channel count (`9ch`) |
| `headmapcapture.csv` | `head_key, manufacturer, model, channel_count, visualiser_name` |
| `wygheads.csv` | WYSIWYG visualiser mapping, same shape |
| `manufacturer_exceptions.csv` | Manufacturer alias table (`adj,americandj`) — used for fuzzy manufacturer matching |
| `heads.exp` | Small export descriptor, e.g. `PP,"All persons","Tue May 26 10:21:11 2026",0,0;` |
