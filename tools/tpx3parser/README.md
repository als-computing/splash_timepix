# tpx3parser — ASI TimePix3 raw file parser

Reads `.tpx3` files written by ASI Luna software and produces HDF5 output
byte-identical to `tpx3dump process` (raw hits + TDC events, no clustering).

Validated row-for-row against `tpx3dump` on six independent files:

| File | Size | Hits | TDC events | ETB | Result |
|------|------|------|------------|-----|--------|
| `sample21MB.tpx3` | 21 MB | 2,682,106 | 0 | — | **all fields exact** |
| `sample1.1GB.tpx3` | 1.1 GB | 56,209,270 | 80,950,407 (7 rollovers) | — | **all fields exact** |
| `000000.tpx3` | 769 MB | 71,515,008 | 24,000,131 | 241 values | **all fields exact** |
| `000005.tpx3` | 808 MB | 76,405,254 | 24,000,132 | 241 values | **all fields exact** |
| `000010.tpx3` | 769 MB | 71,507,704 | 24,000,132 | 241 values | **all fields exact** |
| `000012.tpx3` | 4.0 GB | 138,884,447 | 360,001,966 (39 SPIDR rollovers) | 3601 values | **all fields exact** |

---

## TPX3 packet format (bit-level)

All packets are 8-byte little-endian words. Three packet types matter:

### Section header (`(raw & 0xFFFF_FFFF) == 0x3358_5054`)

```
bits 63..48   section byte count (u16, includes header)
bits 47..40   reserved
bits 39..32   chip_id (u8)
bits 31..0    magic "TPX3" = 0x33585054
```

Some firmware versions follow the header with a `0x50` sequence-counter
packet; others go straight to data. The parser auto-detects.

### Hit packet (top nibble `0xB`)

```
bits 63..60   0xB
bits 59..44   pixel address (u16)
               → dcol = (addr & 0xFE00) >> 8
               → spix = (addr & 0x01F8) >> 1
               → pix  =  addr & 0x0007
               → x    = dcol + (pix >> 2)
               → y    = spix + (pix  & 3)
bits 43..30   toa14 (14-bit coarse ToA, 1.5625 ns unit)
bits 29..20   tot   (10-bit Time-over-Threshold, raw)
bits 19..16   ftoa  (4-bit fine ToA, 0..15)
bits 15..0    spidr (16-bit SPIDR counter)
```

**Luna time unit is 0.1 ps.** Formula verified row-for-row against `tpx3dump`:

```
coarse  = (spidr << 14) | toa14
toa     = (coarse * 16 - ftoa) * 15625   [0.1 ps units]
tot     = tot_raw * 25                    [25 ps units]
```

Row order in `/PixelHits` is `(toa, x, y)` ascending.

### TDC packet (top nibble `0x6`, four subtypes)

The TPX3 chip has two TDC channels, each with a rising and falling edge.
All four subtypes share the same bit layout; only byte 7 differs:

| byte 7 | subtype | `tdc_type` in HDF5 |
|--------|---------|--------------------|
| `0x6F` | TDC1 rising  | 1 |
| `0x6A` | TDC1 falling | 2 |
| `0x6E` | TDC2 rising  | 3 |
| `0x6B` | TDC2 falling | 4 |

Different firmware/acquisition modes use different subsets (e.g. `sample1.1GB`
uses only `0x6E`; the energy-calibration runs use `0x6F` + `0x6A`).

```
bits 63..56   TDC subtype byte (see table above)
bits 55..44   trigger_count (12-bit rolling counter, 0..4095)
bits 43..9    ts35 (35-bit coarse timestamp, 3.125 ns unit)
bits 8..5     fine (4-bit, range 1..12, sub-tick interpolation)
bits 4..0     reserved
```

```
timestamp = ts35 * 31250 + ((fine - 1) * 31250) / 12   [0.1 ps units]
```

### Other packet types (safely ignored)

`0x50` sequence counter, `0x5A`/`0x5C`/`0x5F` global-time/control,
`0x51` chip phase, `0x71` shutter/exposure-boundary.

---

## Target HDF5 layout

All datasets at root, no attributes, no compression,
`chunks = (15000,)`, `maxshape = (None,)`.

### `/PixelHits` — compound, `itemsize = 40`

| field  | type | offset | notes                           |
|--------|------|--------|---------------------------------|
| `toa`  | u64  | 0      | 0.1 ps units                    |
| `cid`  | i64  | 8      | always −1 (clustering disabled) |
| `tot`  | u64  | 16     | 25 ps units                     |
| `dtoa` | i32  | 24     | always −1                       |
| `x`    | u16  | 28     |                                 |
| `y`    | u16  | 30     | 2 bytes implicit padding follow |
| `tof`  | i64  | 32     | always −1                       |

### `/TDCEvents` — compound, `itemsize = 16`

| field           | type | offset | notes                        |
|-----------------|------|--------|------------------------------|
| `tdc_type`      | u8   | 0      | 1–4, see subtype table above |
| _(padding)_     | u8   | 1      |                              |
| `trigger_count` | u16  | 2      | 12-bit rolling counter       |
| _(padding)_     | u32  | 4      |                              |
| `timestamp`     | u64  | 8      | 0.1 ps units                 |

### `/ExposureTimeBoundaries` — `uint64`

Cumulative count of (hits + TDC events) up to each time-bin boundary.
Mirrors `tpx3dump --time-bin-interval`, which defaults to 500 ms.
`tpx3parser` uses the same 500 ms default — **no flag required for standard
acquisitions**.

The boundaries are placed at multiples of the interval starting from `t = 0`
(absolute), followed by a final sentinel equal to the total event count.
Override with `--exposure-interval-ms T` only if `tpx3dump` was called with a
non-default `--time-bin-interval`.

---

## SPIDR rollover handling

The 30-bit coarse counter `(spidr << 14 | toa14)` wraps every ~26.84 s.
Long acquisitions cross this boundary many times. Wraps are detected with a
**large-jump threshold** of 2^29 coarse ticks (~13.4 s):

> A genuine out-of-order packet pair has a sub-µs difference; a rollover
> produces a decrease of ~2^30 ticks. Any decrease larger than 2^29 is a rollover.

This is applied per-section (intra) during parallel decode, then propagated
sequentially across sections for each chip (cross-section). The 35-bit TDC
coarse counter (wraps every ~107.4 s) is handled identically with a 2^34
threshold.

---

## Deviations from rustpix

Partially adapted from [ornlneutronimaging/rustpix](https://github.com/ornlneutronimaging/rustpix)
(`rustpix-tpx`, MIT licence — full notice in `THIRD_PARTY_LICENSES.txt`).
The `Tpx3Packet` struct scaffolding and `pixel_coordinates` formula originate
there; all time-conversion formulas, rollover logic, HDF5 writer, and other
files are original. Four fixes were required to match ASI/Luna semantics:

1. **Hit ToA resolution** — rustpix's `timestamp_coarse()` returns
   `(spidr << 14 | toa14) * 25 ns` (drops `fine_toa` — 16× too coarse).
   Fixed: `((coarse * 16 - ftoa) * 15625)` in 0.1 ps.

2. **TDC timestamp** — rustpix extracts `(raw >> 12) & 0x3FFF_FFFF`, which
   does not match the ASI TDC layout. Fixed: 35-bit `ts35` at bits 9..43
   plus 4-bit `fine` at bits 5..8, with the verified formula above.

3. **Hits gated on TDC** — rustpix silently drops every hit that arrives
   before the first TDC event. Fixed: hits are processed unconditionally
   (many files have zero TDC packets).

4. **u32 overflow / u64 monotone time** — rustpix uses a `u32` timestamp
   relative to the most recent TDC event. Fixed: `u64` monotone ToA with
   independent SPIDR rollover epoch tracking.

---

## Build

```bash
source "$HOME/.cargo/env"
cd tools/tpx3parser
HDF5_DIR=/usr/lib/x86_64-linux-gnu/hdf5/serial cargo build --release
```

Binary: `tools/tpx3parser/target/release/tpx3parser`  
(`tools/tpx3parser/target/` is git-ignored.)

Set `LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/hdf5/serial:$LD_LIBRARY_PATH`
at runtime (or use `hdf5-metno`'s `static` feature to link statically).

---

## Usage

```bash
# Parse and write HDF5 — ExposureTimeBoundaries default to 500 ms intervals
tpx3parser -i input.tpx3 -o output.h5

# Suppress progress output
tpx3parser -i input.tpx3 -o output.h5 --quiet

# Print packet-type statistics
tpx3parser -i input.tpx3 -o output.h5 --stats

# Limit worker threads
tpx3parser -i input.tpx3 -o output.h5 --threads 4

# Non-default exposure interval (only needed if tpx3dump used --time-bin-interval)
tpx3parser -i input.tpx3 -o output.h5 --exposure-interval-ms 250

# Stats only — no output file written
tpx3parser -i input.tpx3 --stats
```

### Finding the exposure interval

`tpx3dump --time-bin-interval` is a runtime flag (not stored in the `.tpx3`
file or `_meta.json`) that defaults to `500ms`. To check what value was used
when generating an existing reference `.h5`:

```python
import h5py, numpy as np
f = h5py.File('reference.h5', 'r')
toa = f['PixelHits']['toa']
n   = f['ExposureTimeBoundaries'].shape[0] - 1        # number of full bins
span_ms = (int(toa[-1]) - int(toa[0])) * 1e-13 * 1e3 # 0.1ps → ms
print(f"interval ≈ {span_ms / n:.1f} ms")
```

---

## Validation

```bash
# Validate ours vs ASI reference (ExposureTimeBoundaries as warning only)
python tools/tpx3parser/validate.py \
    --ours output.h5 \
    --ref  tpx3dump_output.h5

# Strict check including ExposureTimeBoundaries
python tools/tpx3parser/validate.py \
    --ours output.h5 --ref tpx3dump_output.h5 --strict-etb
```

Exit code 0 = all PASS. Non-zero = mismatch. `ExposureTimeBoundaries`
mismatches are warnings by default; add `--strict-etb` to treat them as
failures (requires matching `--exposure-interval-ms`).

---

## Performance

Measured on the development machine (AMD Ryzen, 16 logical cores):

| File | Size | Hits + TDC events | Time | Throughput |
|------|------|-------------------|------|------------|
| `sample21MB.tpx3` | 21 MB | 2.68M | 0.6 s | ~4.5 M hits/s |
| `000000.tpx3` | 769 MB | 71.5M + 24.0M TDC | 16 s | ~4.6 M hits/s |
| `sample1.1GB.tpx3` | 1.1 GB | 56.2M + 81.0M TDC | 24 s | ~2.4 M hits/s |
| `000012.tpx3` | 4.0 GB | 138.9M + 360.0M TDC | 42 s | ~3.3 M hits/s |

Decode runs in parallel via rayon; cross-section epoch propagation and
final `(toa, x, y)` sort are sequential and dominate for large files.

---

## Unit tests

```bash
HDF5_DIR=/usr/lib/x86_64-linux-gnu/hdf5/serial cargo test
```

16 tests cover:

- Bit-field extraction for known literal packets (header, hit, TDC)
- `pixel_coordinates()` round-trip
- `toa_01ps()` formula against row 0 of `sample21MB_PixelHits.h5`
- `tdc_timestamp_01ps()` against row 0 of `sample1.1GB_PixelHits.h5`
- Section discovery (with and without `0x50` sequence packet, fallback scan)
- Decode sort order and no-TDC pass-through
- Rollover handling across empty sections
