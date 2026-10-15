# Tiled on the acquisition PC

How the parquet scans written by `tpx-stream` get into a [Tiled](https://blueskyproject.io/tiled)
catalog, and how to run the server. Design: `info/plans/parquet_streaming_tiled.md` §8.

Decision (first releases): the Tiled server runs **on the acquisition PC**, as the same user as
the UI, and serves the parquet files **in place** — nothing is copied. The UI registers every
finished scan at `http://localhost:8000`; `tpx-tiled-register` does the same from the command
line and catches up anything a Tiled outage missed.

## Install

```bash
pip install -e ".[tiled]"        # client only (tpx-tiled-register, the UI hook)
pip install "tiled[server]"      # on the machine that runs the server
```

The `minimal-server` extra of Tiled 0.2.x cannot import `tiled.server.app`; use `tiled[server]`.

## API key

Tiled requires a key for writing (and, unless `--public`, for reading). It must be
**alphanumeric**:

```bash
mkdir -p ~/.config/splash_timepix
umask 077
echo "TILED_API_KEY=$(openssl rand -hex 32)" > ~/.config/splash_timepix/tiled.env
```

Both the server unit and the UI read this file, so the key lives in exactly one place. It is
**never** stored in `operator_prefs.json`; the UI and `tpx-tiled-register` take it from the
`TILED_API_KEY` environment variable (or `--api-key` on the command line).

## Run the server

Try it by hand first (`<output_dir>` is the operator's output directory, the parent of the
`<scan_uuid>/` directories):

```bash
set -a; . ~/.config/splash_timepix/tiled.env; set +a
mkdir -p ~/.local/share/splash_timepix/tiled
tiled serve catalog --init --api-key "$TILED_API_KEY" \
    --read <output_dir> \
    --host 127.0.0.1 --port 8000 \
    ~/.local/share/splash_timepix/tiled/catalog.db
```

`--read` (`-r`) lists the directories the server may serve files from. It is enforced at
**read** time, not at registration: a scan registered from outside these locations fails at
the first `.read()`, which is why `tpx-tiled-register` reads one partition back after
registering and fails loudly if it cannot. Add every output directory operators use, or one
parent of them all. Drop `--init` after the first start (it is harmless to keep).

### systemd user service

`~/.config/systemd/user/tiled-catalog.service`:

```ini
[Unit]
Description=Tiled catalog for Timepix3 parquet scans
After=network.target

[Service]
EnvironmentFile=%h/.config/splash_timepix/tiled.env
ExecStart=%h/Desktop/tpx3LOCAL/software/splash_timepix_dev/.venv/bin/tiled serve catalog \
    --api-key ${TILED_API_KEY} \
    --read %h/Desktop/data \
    --host 127.0.0.1 --port 8000 \
    %h/.local/share/splash_timepix/tiled/catalog.db
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Adjust the venv path and the `--read` directory, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now tiled-catalog.service
systemctl --user status tiled-catalog.service
loginctl enable-linger "$USER"      # keep it running when nobody is logged in
```

Run `tiled serve catalog --init …` once by hand before enabling the unit so the database exists.

### The UI needs the key too

Launch the UI with the environment file loaded, e.g. in the desktop entry or wrapper script:

```bash
set -a; . ~/.config/splash_timepix/tiled.env; set +a
tpx-ui
```

Without `TILED_API_KEY` the UI still writes parquet; registration fails (logged, non-fatal) and
the catch-up at the next start registers the scans once the key is available.

## Register

Automatic: after every Start or Replay run with parquet on, the UI registers the scan a few
seconds after the streaming server exits (so the UI's `_meta.json` is already written and gets
merged in as the `ui` block). At start-up the UI runs a catch-up over the output directory
(`tpx-ui --no-catch-up` skips it). Preferences: `tiled_uri` (empty string = off) and
`tiled_path` (default `tpx3`).

By hand:

```bash
tpx-tiled-register <output_dir>/<scan_uuid>                 # one scan
tpx-tiled-register <scan_dir> --allow-incomplete            # crashed scan, footer-complete files only
tpx-tiled-register --unregistered --scan-root <output_dir>  # everything without _tiled_registered.json
tpx-tiled-register <scan_dir> --force                       # delete and recreate the node
```

Exit codes: 0 ok, 1 failed (connection, authentication, read-back), 2 refused (incomplete scan
without `--allow-incomplete`, usage error).

Re-running is idempotent: an unchanged file set refreshes the metadata and keeps the node (so
anything people added in Tiled survives); a changed file set recreates it. Success writes
`_tiled_registered.json` into the scan directory; the catch-up looks for scans without it.

### What a scan looks like in Tiled

```
tpx3/
  000042_2f1c9a7b_20261014T153012/     container, spec tpx3_scan — all scan metadata
    events                             table, N partitions = N files
    tdc
    control
```

The key is the UI's slug (`{counter}_{uuid8}_{timestamp}`, the same prefix as the `.tpx3`,
CSV, PNG and `_meta.json` files) when the `_meta.json` is found next to the scan directory,
otherwise the scan uuid.

The container's metadata is built from the parquet **footers**: every per-scan field of
`format.md` (settings, `user`, `detector`, `source`, versions), per table `num_rows`,
`num_files`, `num_row_groups`, `bytes`, `t_first`/`t_last` (from the row-group statistics),
top-level `t_first`/`t_last`/`t_first_s`/`t_last_s`/`num_events` for range searches, the
completion fields from `_scan_meta.json` (`status`, `stop_time_iso`, `acquisition_duration_s`,
`stop_message`), the UI's `_meta.json` as `ui` (with `raw_tpx3_path` and `energy_calibration`),
and `registration` (who, when, which files).

## Search

```python
from tiled.client import from_uri
from tiled.queries import Key

c = from_uri("http://localhost:8000")["tpx3"]            # TILED_API_KEY from the environment
c.search(Key("user.sample") == "Cu foil")
c.search(Key("detector.serval_version") == "3.3.2")
c.search(Key("tdc_frequency_hz") > 500)
c.search(Key("status") == "complete")
t = 1_234_567_890_123                                    # ticks; two chained searches = range
c.search(Key("t_first") <= t).search(Key("t_last") >= t)
c.search(Key("t_first_s") >= 0.5)                        # seconds, for JavaScript clients
```

`Key(...)` matches the direct children of the node being searched, so search `tpx3`, not the
scan containers.

## Read

```python
scan = c["000042_2f1c9a7b_20261014T153012"]
scan.metadata["tables"]["events"]["num_rows"]
df = scan["events"].read_partition(0)                     # one file, as pandas
df = scan["events"].read()                                # the whole table over HTTP — small scans only
```

`.read()` streams the entire table as Arrow IPC. For a 100 GB scan ask Tiled where the files
are and read them directly:

```python
paths = [a.data_uri for a in scan.include_data_sources()["events"].data_sources()[0].assets]
import dask.dataframe as dd
ddf = dd.read_parquet([p.removeprefix("file://") for p in paths])
```

or simply `open_scan(scan.metadata["registration"]["scan_dir"])` from `splash_timepix.parquet_meta`
on a machine that sees the disk. Tiled is the discovery and metadata layer; analysis at scale
reads the files.

## Backup and rebuild

The catalog is one SQLite file (`catalog.db`) holding metadata and file pointers only. Back it
up with the data directory. If it is lost, delete the `_tiled_registered.json` markers and run
`tpx-tiled-register --unregistered --scan-root <output_dir>`: everything is rebuilt from the
files.

## Moving the server to another host later

Change `tiled_uri` in the UI, run the server there with `--read` pointing at the mount of the
data directory, and register with `--force` (the `file://` asset paths must be valid on the
server host). Nothing in the files changes.
