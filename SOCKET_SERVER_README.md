# Socket Data Server

A multi-threaded Python server that reads 5-byte messages from a socket and processes them into numpy arrays.

## Overview

This implementation provides:
- **Thread 1**: Socket listener that accepts connections and reads 5-byte messages
- **Thread 2**: Data processor that converts the messages into numpy arrays
- Thread-safe communication using a queue
- Real-time data processing with callback support

## Architecture

```
Client → Socket → Thread 1 (Reader) → Queue → Thread 2 (Processor) → NumPy Array
```

### Thread 1: Socket Reader
- Listens for incoming connections on specified host/port
- Reads exactly 5 bytes per message from each client
- Puts messages into a thread-safe queue
- Handles multiple clients (one at a time currently)

### Thread 2: Data Processor
- Continuously processes messages from the queue
- Converts 5-byte messages to numbers using struct.unpack
- Appends results to a numpy array (thread-safe)
- Calls optional callback functions with new data

## Usage

### Basic Server Setup

```python
from splash_timepix.socket_server import SocketDataServer

# Create server
server = SocketDataServer(host='localhost', port=8888, buffer_size=1000)

# Optional: Set up callback for new data
def handle_new_data(data_array):
    print(f"Received new data: {data_array}")

server.set_data_callback(handle_new_data)

# Start the server
server.start()

# Get processed data
data = server.get_data_array()
print(f"Total data points: {len(data)}")

# Stop the server
server.stop()
```

### Running the Example

```bash
# Start the server
python -m splash_timepix.example

# In another terminal, run the test client
python -m splash_timepix.test_client
```

## Data Format

The server expects exactly 5 bytes per message. Currently, it interprets them as:
- **Bytes 0-3**: Little-endian unsigned 32-bit integer (main value)
- **Byte 4**: Additional byte (currently unused but available)

You can modify the `_data_processor` method to change how the 5 bytes are interpreted.

### Example Message Formats

```python
import struct

# Send a 32-bit integer (1234) + extra byte (5)
message = struct.pack('<I', 1234) + bytes([5])

# Send a float (3.14) + extra byte (10)
message = struct.pack('<f', 3.14) + bytes([10])

# Send 5 individual bytes
message = bytes([0x01, 0x02, 0x03, 0x04, 0x05])
```

## Test Client

The included test client (`test_client.py`) provides:

1. **Test Mode**: Sends predefined test data
2. **Interactive Mode**: Manual message sending with commands:
   - `send <value> [extra_byte]` - Send specific message
   - `auto <interval> [count]` - Auto-send random messages
   - `stop` - Stop auto-sending
   - `quit` - Exit

## API Reference

### SocketDataServer

#### Constructor
```python
SocketDataServer(host='localhost', port=8888, buffer_size=1000)
```

#### Methods
- `start()` - Start the server and threads
- `stop()` - Stop the server and threads
- `set_data_callback(callback)` - Set callback for new data
- `get_data_array()` - Get copy of current numpy array
- `clear_data_array()` - Clear the data array
- `get_queue_size()` - Get current queue size

## Performance Considerations

- **Buffer Size**: Adjust `buffer_size` based on expected message rate
- **Data Storage**: The numpy array grows continuously; consider periodic clearing
- **Threading**: Currently handles one client at a time; can be extended for multiple concurrent clients
- **Memory Usage**: Monitor memory usage for long-running servers

## Customization

### Changing Data Interpretation

Modify the `_data_processor` method to change how 5-byte messages are processed:

```python
# Example: Treat as 4-byte float + 1-byte flag
value = struct.unpack('<f', message[:4])[0]  # Float
flag = message[4]  # Flag byte

# Example: Treat as 5 individual bytes
bytes_array = np.frombuffer(message, dtype=np.uint8)
```

### Adding Multiple Client Support

Extend `_handle_client` to spawn separate threads for each client:

```python
def _socket_listener(self):
    # ... existing code ...
    while self.running:
        client_socket, client_address = self.server_socket.accept()
        client_thread = threading.Thread(
            target=self._handle_client,
            args=(client_socket,),
            daemon=True
        )
        client_thread.start()
```

## Error Handling

The server includes comprehensive error handling:
- Socket connection errors
- Malformed messages
- Queue overflow
- Thread synchronization issues

All errors are logged using Python's logging module.

## Dependencies

- `numpy` - For array operations
- `threading` - For multi-threading
- `queue` - For thread-safe communication
- `socket` - For network communication
- `struct` - For binary data parsing
