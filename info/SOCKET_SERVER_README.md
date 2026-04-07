# Socket Server Module (`socket_server.py`)

A multi-threaded TCP server for reading TimePix3 detector data and parsing it with **vectorized NumPy** (`splash_timepix.parser`). This is the implementation used by `app.py` and exported as `SocketDataServer` from the `splash_timepix` package.

## Architecture

`SocketDataServer` splits I/O and parsing across two threads:

### Thread 1: Socket listener (`_socket_listener`)
- Listens for incoming TCP connections on the configured host/port
- Reads data in large chunks, accumulates bytes, and pushes **raw batches** (multiples of 12 bytes) to a thread-safe queue
- Uses a short idle timeout (~0.1 s) to flush partial batches so low-rate streams still produce timely callbacks
- **Blocked on**: `socket.recv()` and socket timeouts

### Thread 2: Data processor (`_data_processor`)
- Pulls byte batches from the queue
- Parses each batch with `PacketParser.parse_batch()` from `splash_timepix.parser`
- Invokes the user callback with a **`BatchParseResult`** (NumPy arrays for pixels, TDCs, controls)
- **Blocked on**: `queue.get()`

```
TCP Socket → Socket Listener → Queue (byte batches) → Data Processor → Callback(BatchParseResult)
              (chunked read)     (thread-safe)           (parse_batch)
```

## Packet types

The binary layout is the same 96-bit (12-byte) TimePix3 live format:

- **Pixel**: position (x, y), time-over-threshold (ToT), timestamp
- **TDC**: channel, edge (rise/fall), timestamp
- **Control**: subtype (shutter, heartbeat, etc.), timestamp

Field definitions and single-packet helpers live in **`parser.py`** (`PixelPacket`, `TDCPacket`, `ControlPacket`, and `PacketParser.parse` for one 12-byte packet).

## Callback contract

The callback receives **`BatchParseResult`**, not a list of Python packet objects:

```python
from splash_timepix.socket_server import SocketDataServer
from splash_timepix.parser import BatchParseResult

def my_callback(result: BatchParseResult) -> None:
    """Called once per parsed batch on the data processor thread."""
    if result.n_pixels:
        xs = result.pixel_x
        ys = result.pixel_y
        print(f"{result.n_pixels} pixels, first=({xs[0]}, {ys[0]})")
    if result.n_tdc:
        print(f"{result.n_tdc} TDC events")

server = SocketDataServer(
    host="localhost",
    port=9090,
    buffer_size=1000,
    callback_batch_size=10000,  # target packet count per *read* batch (×12 bytes)
)
server.set_data_callback(my_callback)
server.start()
# ...
server.stop()
```

**Important:** The callback runs **synchronously** on the data processor thread. Keep it fast (binning, counting, enqueue to another thread). Slow work (disk, plotting) should be offloaded.

## Basic usage example

```python
from splash_timepix.socket_server import SocketDataServer
import time

server = SocketDataServer(host="localhost", port=9090)
total_pixels = 0

def count_pixels(result):
    global total_pixels
    total_pixels += result.n_pixels
    if total_pixels and total_pixels % 100_000 < result.n_pixels:
        print(f"Processed ~{total_pixels} pixel events")

server.set_data_callback(count_pixels)
server.start()
time.sleep(60)
print("Queue size:", server.get_queue_size())
server.stop()
```

## API reference

### `SocketDataServer(...)`

**Parameters**

| Parameter | Description |
|-----------|-------------|
| `host` | Bind address (default `localhost`) |
| `port` | TCP port (default `9090`) |
| `buffer_size` | Max queued **batches** before `put` may block or drop |
| `debug` | If `True`, fills a small ring buffer of human-readable batch summaries |
| `callback_batch_size` | Target number of **packets** per read batch; internal byte size is `callback_batch_size * 12` |
| `exit_on_disconnect` | If `True`, stop the server when the client disconnects |

### Methods

- **`start()`** — Start listener and processor threads (non-blocking).
- **`stop()`** — Stop threads and close sockets (waits up to ~5 s per thread).
- **`set_data_callback(callback)`** — `callback: Callable[[BatchParseResult], None]`.
- **`get_queue_size()`** — Current depth of the internal batch queue.
- **`get_callback_buffer_size()`** — Always **`0`** in this implementation (no separate pre-callback packet list; batching is byte-based upstream).
- **`get_unknown_packet_count()`** — Cumulative unknown/invalid packet types from `parse_batch`.
- **`get_valid_packet_samples()`** — Last debug strings if `debug=True`, else `[]`.
- **`wait_for_client_disconnect(timeout=None)`** — Block until the client disconnects event is set.

## Thread safety

| Piece | Notes |
|-------|--------|
| `message_queue` | Standard `queue.Queue`, safe across threads |
| Callback | Runs only on the data processor thread; synchronize any shared state you update |

## Performance notes

- Larger **`callback_batch_size`** → fewer, larger TCP read batches and fewer `parse_batch` calls (good for throughput; slightly higher latency per batch).
- If **`buffer_size`** is too small under load, batches may be dropped with a warning.
- For object-oriented per-packet code paths, use `PacketParser.parse_stream` or `parse_batch_to_objects` in `parser` on data you already buffered (not on the hot socket callback path).

## Data format

- **Input:** TCP stream of 12-byte packets (96-bit ASI TimePix3 live format).
- **Output to callback:** One **`BatchParseResult`** per queue item (aligned NumPy arrays; see `parser.BatchParseResult`).

Further format detail: **`parser.py`** and ASI TimePix3 documentation.

## Error handling

- Socket and queue errors are logged; threads try to stay alive where possible.
- Exceptions in the user callback are logged; the processor loop continues.

## Debug mode

With **`debug=True`**, short string summaries of recent batches are stored for `get_valid_packet_samples()`, and logging can be more verbose. Use while bringing up clients or checking that pixels/TDCs arrive as expected.
