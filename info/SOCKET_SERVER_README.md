# Socket Server Module (`socket_server.py`)

A multi-threaded TCP socket server for reading and parsing TimePix3 detector data packets.

## Architecture

The `SocketDataServer` uses a **two-thread architecture** to separate I/O-bound and CPU-bound work:

### Thread 1: Socket Listener (`_socket_listener`)
- Listens for incoming TCP connections on specified host/port
- Reads exactly 12-byte packets from connected clients
- Places raw bytes into a thread-safe queue
- **Blocked on**: `socket.recv()` (network I/O)

### Thread 2: Data Processor (`_data_processor`)
- Retrieves raw bytes from the queue
- Parses bytes into typed packet objects using `PacketParser`
- Batches packets and calls user-provided callback
- **Blocked on**: `queue.get()` (waiting for data)
```
TCP Socket → Socket Listener → Queue → Data Processor → Callback
             (12 bytes)        (thread-safe)  (parse & batch)  (user code)
```

## Packet Types

The server parses three types of 96-bit (12-byte) packets:

- **PixelPacket**: Photon detection events with (x, y) position and time-over-threshold (ToT)
- **TDCPacket**: Time-to-digital converter triggers (rising/falling edge on channels 1 or 2)
- **ControlPacket**: Control messages (shutter, heartbeat, timestamp)

See `parser.py` for packet structure details.

## Callback Support

The server supports a user-provided callback function that receives **batched packets**:
```python
from splash_timepix.socket_server import SocketDataServer
from splash_timepix.parser import PixelPacket, TDCPacket

def my_callback(packets):
    """
    Called when callback_batch_size packets have been buffered.
    
    Args:
        packets: List of parsed packet objects (PixelPacket, TDCPacket, ControlPacket)
    """
    for packet in packets:
        if isinstance(packet, PixelPacket):
            print(f"Pixel at ({packet.x}, {packet.y})")
        elif isinstance(packet, TDCPacket):
            print(f"TDC trigger on channel {packet.channel}")

# Create server
server = SocketDataServer(
    host='localhost',
    port=9090,
    buffer_size=1000,
    callback_batch_size=1000  # Batch size for performance
)

# Set callback
server.set_data_callback(my_callback)

# Start server
server.start()

# ... run for some time ...

# Stop server
server.stop()
```

**Important**: The callback runs **synchronously** on the Data Processor thread. Keep callback logic fast to avoid blocking packet processing.

## Basic Usage Example
```python
from splash_timepix.socket_server import SocketDataServer
from splash_timepix.parser import PixelPacket
import time

# Create and start server
server = SocketDataServer(host='localhost', port=9090)

# Simple callback: count packets
packet_count = 0

def count_packets(packets):
    global packet_count
    for packet in packets:
        if isinstance(packet, PixelPacket):
            packet_count += 1
    
    if packet_count % 10000 == 0:
        print(f"Processed {packet_count} pixel events")

server.set_data_callback(count_packets)
server.start()

print("Server listening on localhost:9090")
print("Waiting for data...")

# Run for 60 seconds
time.sleep(60)

# Get statistics
queue_size = server.get_queue_size()
print(f"Total pixel events: {packet_count}")
print(f"Queue size: {queue_size}")

# Clean shutdown
server.stop()
```

## API Reference

### `SocketDataServer`

#### Constructor
```python
SocketDataServer(
    host='localhost',
    port=9090,
    buffer_size=1000,
    debug=False,
    callback_batch_size=1000
)
```

**Parameters:**
- `host` (str): Host address to bind to
- `port` (int): Port number to listen on
- `buffer_size` (int): Maximum number of messages in internal queue
- `debug` (bool): Enable packet buffer and detailed logging
- `callback_batch_size` (int): Number of packets to batch before calling callback

#### Methods

**`start()`**
- Starts the socket listener and data processor threads
- Non-blocking: returns immediately

**`stop()`**
- Stops all threads and closes sockets
- Waits up to 5 seconds for threads to finish

**`set_data_callback(callback: Callable)`**
- Sets callback function to receive parsed packets
- Callback signature: `def callback(packets: List[Packet]) -> None`
- Callback is called when `callback_batch_size` packets have been buffered
- Callback also called on queue timeout (every 1 second) to flush partial batches

**`get_queue_size() -> int`**
- Returns current number of raw messages (12-byte packets) in queue

**`get_callback_buffer_size() -> int`**
- Returns current number of parsed packets in callback buffer (not yet delivered)

**`get_unknown_packet_count() -> int`**
- Returns count of packets with unknown/invalid packet type

**`get_valid_packet_samples() -> List[str]`**
- Returns last 10 valid packets as formatted strings (debug mode only)
- Returns empty list if `debug=False`

## Thread Safety

The following components are **thread-safe**:

| Component | Thread-Safe? | Mechanism |
|-----------|--------------|-----------|
| `message_queue` | Yes | Built-in `queue.Queue` locks |
| `callback_buffer` | Yes | Only accessed by data processor thread |
| Callback execution | User responsibility | Runs on data processor thread |

**For callback writers:** If your callback modifies shared state, you must use proper synchronization (locks, queues, etc.).

## Performance Considerations

- **Batch size**: Larger `callback_batch_size` reduces callback overhead but increases latency
  - Use 1 for real-time, event-by-event processing
  - Use 1000+ for high-throughput applications
- **Queue overflow**: If `buffer_size` is too small, incoming packets will be dropped with warnings
- **Callback speed**: Slow callbacks block the data processor thread and can cause queue backlog
- **Recommended pattern**: Use the callback only for fast operations (binning, counting). For slow operations (plotting, file I/O, network publishing), queue data to another worker thread.

## Data Format

- **Input**: 12-byte packets via TCP socket (96-bit TimePix3 format)
- **Output**: List of parsed `Packet` objects delivered to callback

For packet structure and binary format details, see `parser.py` and the ASI TimePix3 documentation.

## Error Handling

The server logs errors using Python's `logging` module:

- **Socket errors**: Connection failures, broken pipes
- **Queue overflow**: Warnings when messages are dropped due to full queue
- **Parse errors**: Unknown packet types logged with raw hex data

All threads handle exceptions gracefully and continue operation when possible.

## Debug Mode

When `debug=True`:
- Enables packet buffer tracking (last 10 valid packets)
- Logs detailed information about received packets
- Ring buffer captures WARNING+ messages for display

Use debug mode during development to inspect packet flow and diagnose issues.