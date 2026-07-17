# Sniffer — TCP Pipeline Diagnostics

A set of tools for diagnosing bursty or "bolus" data-flow problems in the
Serval → live-cli → splash\_timepix.app TCP pipeline.

---

## Background & Objective

The TimePix3 acquisition pipeline moves data over two local TCP hops:

```
Serval  ──(port 7070)──►  live-cli  ──(port 9090)──►  splash_timepix.app
```

A recurring symptom is that data arrives in large bursts (~25 MiB boluses)
rather than smoothly, causing the downstream application to stall between
bursts.  The sniffer tools answer two questions:

1. **Where is the bolus introduced?** — Is Serval emitting bursty data, or is
   live-cli buffering a smooth input and then flushing it all at once?
2. **Does the sort-buffer size in live-cli control the bolus size?** — A 3×3
   parameter sweep (`run_sniff_sweep.sh`) tests this hypothesis.

---

## How it works

```
┌─────────────────────────────────────────────────────────────────┐
│  run_sniff_experiment.sh                                        │
│                                                                 │
│  [1] tcpdump -i lo  →  /tmp/sniff_NNN.pcap                     │
│  [2] splash_timepix.app (port 9090, ZMQ 5657, HB 5658)         │
│  [3] live-cli (port 7070 → 9090)                               │
│  [4] flush_pacing_listener.py  →  /tmp/flush_pacing_NNN.json   │
│  [5] splash_timepix.serval_client.acq  (timed acquisition)      │
│  [teardown] → sniff_analyze.py (pcap + listener JSON → report) │
└─────────────────────────────────────────────────────────────────┘
```

1. `tcpdump` captures all TCP traffic on the loopback interface for both ports.
2. `flush_pacing_listener.py` subscribes to the app's ZMQ and heartbeat ports
   and records the timestamp of every flush event.
3. After acquisition, `sniff_analyze.py` parses the pcap and the listener JSON,
   aligns them on a shared wall-clock origin, and prints:
   - An ASCII timeline (bytes/s per bin for hop A and hop B, plus flush counts)
   - A **diagnosis** classifying whether the bolus source is Serval, live-cli,
     or something else

---

## Files

| File | Description |
|---|---|
| `run_sniff_experiment.sh` | Orchestrates a single timed acquisition end-to-end |
| `run_sniff_sweep.sh` | Runs a 3×3 grid of live-cli buffer knobs (`--bin-width-exp` × `--max-delay-bins`) |
| `sniff_analyze.py` | Parses pcap + listener JSON; prints aligned timeline + diagnosis |
| `flush_pacing_listener.py` | Subscribes to ZMQ/heartbeat ports; records flush event timestamps |

---

## Requirements

- `tcpdump` (may need `sudo` — see below)
- Python packages: `dpkt` (pcap parsing), `pyzmq` (ZMQ subscriber)
- `splash_timepix.app`, `live-cli`, and `splash_timepix.serval_client.acq`
  must be reachable in the active environment

Install missing Python deps:

```bash
.venv/bin/python3.12 -m pip install dpkt pyzmq
```

---

## Quick start: single experiment

```bash
# From the project root, with the UI already running (--autostart-serval):
bash tools/sniffer/run_sniff_experiment.sh
```

The script blocks until the acquisition completes, tears everything down, and
immediately runs `sniff_analyze.py` to print the diagnosis.

Artifacts written to `/tmp/`:

```
/tmp/sniff_<timestamp>.pcap
/tmp/flush_pacing_<timestamp>.json
/tmp/sniff_logs_<timestamp>/
  tcpdump.log
  app.log
  livecli.log
  listener.log
  acq.log
```

### Environment variable overrides

| Variable | Default | Description |
|---|---|---|
| `DURATION_S` | `90` | Acquisition duration in seconds |
| `TDC_FREQ` | `1000` | TDC frequency (Hz) passed to the app |
| `FLUSH_INTERVAL` | `1.0` | App flush interval (seconds) |
| `LIVECLI_BIN_WIDTH_EXP` | `0` | live-cli `--bin-width-exp` knob |
| `LIVECLI_MAX_DELAY_BINS` | `1` | live-cli `--max-delay-bins` knob |
| `TAG` | `default` | Label appended to artifact filenames |

Example:

```bash
DURATION_S=120 TDC_FREQ=500 TAG=slow bash tools/sniffer/run_sniff_experiment.sh
```

---

## Parameter sweep

`run_sniff_sweep.sh` runs 9 experiments in sequence, sweeping:

- `--bin-width-exp` ∈ {0, 10, 100}
- `--max-delay-bins` ∈ {1, 10, 100}

Each run prints a `RESULT` summary line; at the end, all results are collected
into a table showing the bolus size for each combination.

```bash
DURATION_S=60 bash tools/sniffer/run_sniff_sweep.sh
```

Expected output (per run):

```
RESULT idx=1/9 tag=bwe0_mdb1 bwe=0 mdb=1 status=OK bolus=26214400 n_pkts_A=... n_pkts_B=...
```

If `bolus` scales with `max-delay-bins`, the sort buffer in live-cli is the
source.  If it stays constant, the buffer is elsewhere (e.g. Serval itself).

---

## Analyzing an existing pcap

```bash
.venv/bin/python3.12 tools/sniffer/sniff_analyze.py /tmp/sniff_NNN.pcap
.venv/bin/python3.12 tools/sniffer/sniff_analyze.py /tmp/sniff_NNN.pcap \
    --listener /tmp/flush_pacing_NNN.json \
    --bin 0.5 \
    --gap-threshold 3.0
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--listener PATH` | (none) | Path to `flush_pacing_listener.py` JSON artifact |
| `--bin SECONDS` | `1.0` | Time-bin width for the timeline |
| `--gap-threshold SECONDS` | `2.0` | Minimum silence gap to report |

---

## Diagnosis interpretation

After printing the timeline, `sniff_analyze.py` emits one of four verdicts:

| Hop A (Serval→live-cli) | Hop B (live-cli→app) | Verdict |
|---|---|---|
| Bursty | Bursty (lock-step) | **Serval is the source** — live-cli faithfully forwards |
| Smooth | Bursty | **live-cli is buffering its output** |
| Bursty | Smooth | Unexpected — live-cli is de-bursting upstream (unlikely) |
| Smooth | Smooth | No bolus detected at TCP level — check inside the app |

---

## Notes on `tcpdump` permissions

On most Linux systems, `tcpdump` on the loopback requires `root` or the
`CAP_NET_RAW` capability.  The experiment script will attempt to run it
directly; if it fails, prefix with `sudo`:

```bash
sudo bash tools/sniffer/run_sniff_experiment.sh
```
