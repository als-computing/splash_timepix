# Solution: Smoothing the Flood of ZMQ Flush Messages

## Context

### Symptom

When the streaming server (`splash_timepix/app.py`) is running behind
`luna-iterator`, the ZMQ flush messages arrive on the subscriber (the Engineering
tab / Operator tab) in **bursts** rather than at the configured rate. Example
from a real session with `tdc_frequency = 1 Hz` and `flush_interval = 1 s`:

```
[13:35:42] Flush #1:  1 cycles, 1.15e+05 counts
[13:35:42] Flush #2:  1 cycles, 1.13e+05 counts
[13:35:42] Flush #3:  1 cycles, 1.15e+05 counts
...
[13:35:43] Flush #18: 1 cycles, 1.13e+05 counts
[13:35:52] Flush #19: 1 cycles, 1.24e+05 counts
...
```

18 flushes in ~1 s, then silence, then another clump. Expected behaviour: one
flush per wall-clock second.

### Downstream consequences

- **UI freezes briefly** whenever a burst lands. The operator tab's
  `on_flush_received` runs on the GUI thread and does ~40–60 MB of transient
  numpy allocations plus two full heatmap renders per message. A 10-flush burst
  blocks the Qt event loop until the whole backlog drains, then only the *last*
  paint is visible (Qt coalesces `update()` calls).
- **ZMQ back-pressure / drops.** The PUB socket in `workers.py` uses
  `SNDHWM = 10`. If the subscriber lags briefly under a burst, messages are
  silently dropped (`zmq.Again`).
- **Wasted bandwidth.** Bursting sends the same information in N tiny messages
  instead of 1 aggregated one. At a ~10 MB array per flush, a 10× factor is
  meaningful.

## Root cause

The server emits a flush **in reaction to a TDC pulse**, not on a wall-clock
timer. In `splash_timepix/app.py` inside `data_callback → handle_tdc`:

```python
# Check if we need to flush
if cycle_count > 0 and cycle_count % flush_every_n_cycles == 0:
    with xyt_lock:
        xyt_array += local_accumulator
        array_copy = xyt_array.copy()
        xyt_array.fill(0)
    ...
    xyt_queue.put_nowait((array_copy, flush_metadata))
```

The upstream producer (`luna-iterator`) is a multi-stage sorter. Because
sorting requires buffering (to guarantee temporal ordering before emitting),
its sink releases bundles of already-sorted packets at once, e.g. a few
seconds' worth of detector data in a single TCP write. When that bundle hits
the Python server, `data_callback` walks through it, encounters many TDCs
back-to-back, and `handle_tdc` fires one `xyt_queue.put_nowait(...)` **per
TDC**. `zmq_worker` drains that queue immediately and PUBs the messages in a
tight burst.

The TDC pulses themselves are the **data content**; using them as the
**release signal** inadvertently makes the server's output cadence track the
upstream batching rhythm.

## Constraints

- We **cannot** change `luna-iterator`'s chunking; sorting inherently requires
  buffering, and it is not ours to modify.
- We **must** keep wire compatibility with existing ZMQ subscribers (UI,
  `example_listener.py`, and any external consumer).
- We **must not** change the physics: pixel-to-TDC time binning, `t_zero`
  handoff, and the running average must remain exactly correct.

## Solution

Decouple **when data is released** from **when a TDC arrives**. Keep the
existing accumulator as the aggregation buffer; replace the "flush on every
N-th TDC" condition with a **wall-clock gate** sized by `flush_interval`.

### Mental model

The accumulator (`local_accumulator` + `xyt_array`) is already a fixed-size
grid of `uint32` counts — per `(x, y, t_bin)` or `(x, t_bin)` when
`collapse_y=True`. It cannot "overflow" in the usual buffer sense: additional
hits just increment existing cells. Waiting longer before emitting does **not**
cost memory; it only delays the release.

Today a gatekeeper opens the valve "every time a log (TDC pulse) floats by".
With the fix, the gatekeeper has a **wristwatch** and opens the valve
**at most once per `flush_interval`**, tagging each release with the number of
TDC cycles absorbed since the previous release.

### Behavioural guarantees after the fix

- **Total counts preserved.** The accumulator is never discarded, only
  released on a different schedule.
- **Running average preserved.** The UI already weighs each flush by
  `cycles_in_flush` (`operator_tab.py`, `on_flush_received`), so collapsing
  three cycles into one flush with `cycles_in_flush=3` yields the same average
  as three flushes with `cycles_in_flush=1`.
- **Time binning unchanged.** `t_zero` still updates on every valid TDC, so
  each pixel is still binned against the correct cycle's trigger.
- **Wire format unchanged.** Same multi-part ZMQ messages, same metadata
  keys. Only the *values* of `cycles_in_flush` and `flush_number` shift to a
  more regular distribution.

## Design

### What stays the same

- `SocketDataServer`, TCP receive loop, parser — untouched.
- `data_callback` still interleaves TDCs and pixels by index and still calls
  `bin_pixels(...)` for each range.
- `zmq_worker` untouched. Same multipart message (`metadata` + `array_bytes`),
  same `msg_type` semantics, same `SNDHWM`.
- `TimePixStart` / `TimePixStop` control-message behaviour untouched.
- `do_final_flush()` still runs at stop sites and still releases any residual
  data before the `stop` message.
- UI subscriber (`ZmqSubscriberWorker`, `on_flush_received`) — **no change
  required**. It already computes `avg = cumulative / total_cycles`.

### What changes — server side only, localised to `app.py`

1. **New state variables** next to the existing `cycle_count`, `flush_count`,
   etc.:

    - `last_flush_time: Optional[float]` — `time.monotonic()` stamp of the
      previous emission. `None` at startup and on each reconnect; armed on the
      first TDC.
    - `cycles_since_last_flush: int` — incremented in `handle_tdc`, reset on
      each emit. Used as the new `cycles_in_flush` metadata value.

2. **Extract an `emit_flush(cycles_in_flush: int)` helper** that contains
   exactly the copy/zero/put_nowait block currently inlined in `handle_tdc`.
   No logic change; just factoring. This lets both callers (below) share it.

3. **Replace the gate in `handle_tdc`** (`app.py` around line 431):

    ```python
    # OLD:
    if cycle_count > 0 and cycle_count % flush_every_n_cycles == 0:
        ... inline flush ...

    # NEW:
    cycles_since_last_flush += 1
    if last_flush_time is None:
        last_flush_time = time.monotonic()
    elif (time.monotonic() - last_flush_time) >= flush_interval \
         and cycles_since_last_flush > 0:
        emit_flush(cycles_in_flush=cycles_since_last_flush)
        last_flush_time = time.monotonic()
        cycles_since_last_flush = 0
    ```

4. **Add a watchdog in the main loop** (`app.py` around the existing
   `time.sleep(1); current_time = time.time()` block, ~line 681). The main
   loop already ticks once per second; use it to guarantee a release even when
   no TDC arrives:

    ```python
    if (
        last_flush_time is not None
        and cycles_since_last_flush > 0
        and (time.monotonic() - last_flush_time) >= flush_interval
    ):
        emit_flush(cycles_in_flush=cycles_since_last_flush)
        last_flush_time = time.monotonic()
        cycles_since_last_flush = 0
    ```

5. **Reset on client reconnect.** The existing reconnect block (the
   `server.client_connected and not was_client_connected` branch) already
   resets `cycle_count`, `flush_count`, `t_zero`, etc. Add:

    ```python
    last_flush_time = None
    cycles_since_last_flush = 0
    ```

6. **`do_final_flush` metadata nit.** Pass `cycles_in_flush=
   cycles_since_last_flush` explicitly instead of the current
   `cycle_count % flush_every_n_cycles`. More accurate and keeps the same
   meaning for downstream.

### Thread-safety

- Writers of the new state: `data_callback` thread (`handle_tdc`), main
  thread (watchdog + `do_final_flush`).
- The existing `xyt_lock` already protects `xyt_array`. Extend the locked
  region in `emit_flush()` to also cover the gate-state updates
  (`last_flush_time`, `cycles_since_last_flush`), so the read-check-emit
  sequence is atomic. **No new lock is introduced.**

### Timing reference

Use `time.monotonic()` for `last_flush_time` to immunise against NTP jumps or
DST changes. The `flush_metadata["timestamp"]` stamped by `zmq_worker` for
downstream consumers stays as `time.time()` (Unix wall clock) — this is the
one subscribers may already be relying on.

### Edge cases handled

- **Startup with no TDCs yet.** `last_flush_time` stays `None`; the watchdog
  and the gate both short-circuit. First TDC arms the clock; nothing is ever
  emitted with zero cycles.
- **Long silences.** Watchdog in the main loop fires every second and emits
  the last cycle(s) as soon as `flush_interval` has elapsed, even if
  `luna-iterator` has paused.
- **Empty windows.** If no TDC arrived in the last `flush_interval`,
  `cycles_since_last_flush == 0` → no message is sent. Downstream never
  receives a spurious zero flush.
- **Client reconnect.** Gate state is reset alongside the existing scan/state
  reset, so the new scan starts from a clean clock.
- **Shutdown.** `do_final_flush` runs as today and captures any residual data
  before the `stop` message.
- **`flush_interval < 1 / tdc_frequency`.** The existing warning in `app.py`
  still applies. With the new logic, if the interval is shorter than the TDC
  period, the wall-clock gate will always be past due by the time each TDC
  arrives, degenerating gracefully to "flush on every TDC" (which is what the
  warning already promises).

## Expected impact

Using the session in the symptom example (30 flushes observed in ~12 s, each
~10 MB):

|                                    | Before      | After       | Ratio |
| ---------------------------------- | ----------- | ----------- | ----- |
| ZMQ messages / 12 s                | 30          | 12          | 2.5×  |
| Peak burst (messages within 1 s)   | up to ~18   | 1           | ≥18×  |
| Transient UI heap / 12 s           | ~1.2 GB     | ~0.48 GB    | 2.5×  |
| Risk of `zmq.Again` drops          | yes         | effectively no |  —  |
| End-to-end latency of a single flush | ~immediate | +½ × flush_interval | slight regression |

The latency regression (worst case ≈ `flush_interval`, average ≈
`flush_interval / 2`) is intentional and acceptable at 1 Hz.

## Risk & rollback

- Change is confined to `splash_timepix/app.py`. Touches only the flush gate
  condition, a small helper refactor, and the main-loop watchdog.
- Reverting is a few-line patch.
- No migration is required on the subscriber side: `cycles_in_flush` was
  already variable per message shape and is already honoured by the UI and
  `example_listener.py`.

## Out-of-scope / future improvements

Not required to fix this problem, but worth considering independently:

1. **UI coalescing.** Decouple "data accounting" (cheap, every flush) from
   "pixel pushing" (expensive). Keep a pending slot and repaint via a
   QTimer at, say, 10 Hz. Makes the UI robust against bursts even if some
   future data source ignores the server's pacing.
2. **Move running-average math off the GUI thread.** Compute
   `cumulative_sum` and `avg_2d` in `ZmqSubscriberWorker`; hand finished
   arrays to the GUI thread.
3. **Pre-allocated float64 buffer** in `operator_tab.py` instead of
   `array.astype(np.float64)` per flush, to remove the ~20 MB per-flush heap
   churn.
4. **Second, conflated SUB socket** (`zmq.CONFLATE`) just for the
   "current flush" viewport — always-latest, never backlog. The primary SUB
   remains non-conflated for the running average.

These are optional; the primary fix described above is expected to make the
observed flood-of-messages symptoms disappear entirely.
