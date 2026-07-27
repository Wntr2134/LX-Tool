# ChamSys `.hed` head files — format

**Status: solved.** `.hed` files are read and written directly. This document
records the obfuscation scheme, the personality layout, and how the scheme was
recovered.

## The obfuscation

Each character is XORed with a keystream that counts **down** by one per
character, and the result has its high bit set:

```
key(i) = (-i) mod 127        # note: modulo 127, not 128
cipher = (plain XOR key) | 0x80
```

with two details that matter:

* **Newlines are literal `0x0A` and do not advance the counter.** Only
  payload characters increment `i`.
* **A key of 0 is written as 127** at every position except the very first.
  So `i = 0` uses key 0, but `i = 127, 254, 381 …` use key 127.

The scheme is symmetric, so one routine both encodes and decodes —
`decode_hed()` and `encode_hed()` in `lxtool/formats/chamsys.py`.

### Why it resisted analysis

The modulus is **127, not 128**. Every brute-force search over a 128-value
keyspace — single-byte XOR, additive, affine, position-linear — misses it,
because the keystream drifts by one relative to any mod-128 model roughly
every 127 characters. Index-of-coincidence testing also came back flat at
random for every period up to 48, since the true period is 127 and the
keystream never repeats within a short window.

What was established from ciphertext alone, before the break:

* payload bytes occupy exactly 128 distinct values in `0x80`–`0xFF`, so each
  carries 7 bits
* the keystream is position-locked, not chained — an isolated difference
  between two otherwise-identical files does not cascade
* byte frequency is flat, ruling out any monoalphabetic substitution

### How it was broken

With a known-plaintext file: a head built in the MagicQ Head Editor with 20
channels all named `AAAAAAAA`. Across the eight `A`s of one channel name the
recovered key ran `38, 37, 36, 35, 34, 33, 32, 31` — a clean decrement, which
identified the operation as XOR against a down-counter. Checking
`(key + index) mod 127 == 0` across all twenty lines then pinned the modulus
and the phase exactly.

That file is kept as a regression fixture at
`tests/data/test_test_AAAAAAAA.hed`.

## Personality layout

A decoded `.hed` is plain 7-bit ASCII:

```
# MagicQ personality file.  Copyright Chamsys Ltd 2021 www.chamsys.co.uk
\ Personality file for China 7x9 Watt Mini Led par RGB
V,008c,"MagicQ 1";
P,0008,"China_7x9WMiniParRGB_7ch","China","7ch","7x9WMiniParRGB",
0007,0007,000a,0000,0000,0000,0001,0001,01fa,...
"Dimmer",00000001,00000000,
"Red",00002022,00000010,
"Green",00002022,00000011,
```

| Line | Meaning |
|---|---|
| `#` / `\` | Comments |
| `V,<hex>,"MagicQ 1";` | File version |
| `P,<hex>,"<name>","<manufacturer>","<mode>","<model>",` | Header. Note the order: manufacturer, then **mode**, then model |
| next line | First field is the channel count |
| `"<name>",<flags>,<attribute>,` | One per channel, in patch order |

### Channel rows

* **flags** — `(encoder_bank << 4) | type`, where type `1` is HTP and `2` is
  LTP. Encoder banks seen: `0` intensity, `1` beam, `2` colour, `3` position.
  Higher bits are used too (RGB channels carry `0x2000`).
* **attribute** — MagicQ's own parameter id.
* **16-bit** parameters are written as *two consecutive rows sharing a name
  and attribute number*; the second is the fine half.

### Attribute numbers observed

| # | Parameter | | # | Parameter |
|---|---|---|---|---|
| `0x00` | Dimmer | | `0x10` | Red |
| `0x02` | Shutter | | `0x11` | Green |
| `0x04` | Pan | | `0x12` | Blue |
| `0x05` | Tilt | | `0x13` | White |
| `0x06` | Colour wheel | | `0x1A` | Strobe |
| `0x08` | Gobo 1 | | `0x26` | Macro |
| `0x0E` | Prism | | `0x27` | Reset |
| `0x3F` | Reserved (63) — carries no meaning | | | |

This table is **corroboration, not the primary signal**. Real personalities
misuse attribute numbers — one shipping fixture puts `Speed` on `0x02`, which
the table calls Shutter — so the parser normalises the human-readable channel
*name* first and falls back to the number only when the name is unrecognised.

## Writing

`encode_hed()` is exact and proven by round-trip against a real file.
`build_personality()` emits the header and channel block, which are modelled
directly on real personalities.

A `.hed` also carries trailing sections (palettes, ranges, per-channel
defaults) whose meaning has **not** been fully decoded. What is written is the
minimum needed to describe the DMX layout, so treat `.hed` output as
experimental: check it in the Head Editor first. GDTF import remains the
route with no unknowns in it.

## Other plain-text files in the `heads` folder

| File | Contents |
|---|---|
| `<Manufacturer>_<Model>_<Mode>.hed` | Filename mirrors the header fields |
| `headmapcapture.csv` | `head_key, manufacturer, model, channel_count, visualiser_name` |
| `wygheads.csv` | WYSIWYG visualiser mapping, same shape |
| `manufacturer_exceptions.csv` | Manufacturer alias table (`adj,americandj`) |
| `heads.exp` | Export descriptor, e.g. `PP,"All persons","Tue May 26 10:21:11 2026",0,0;` |
