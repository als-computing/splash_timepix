# Socket Data Server

A multi-threaded Python server that reads messages from a socket and processes them into numpy arrays.

## Overview

This implementation provides:
- **Thread 1**: Socket listener that accepts connections and reads 5-byte messages
- **Thread 2**: Data processor that converts the messages into numpy arrays
- Thread-safe communication using a queue
- Real-time data processing with callback support

## Architecture

```
Source → Socket → Thread 1 (Reader) → Queue → Thread 2 (Processor) → NumPy Array
```

### Thread 1: Socket Reader
- Listens for incoming connections on specified host/port
- Reads exactly 12 bytes per message from each source
- Puts messages into a thread-safe queue
- Handles multiple sources (one at a time currently)

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
print(f"Total number of events: {np.sum(data)}")

# Stop the server
server.stop()
```

### Running the Example

```bash
# Start the server
python -m splash_timepix.example

# In another terminal, run the test source
python -m splash_timepix.test_source
```

## Data Format

The server expects exactly 12 bytes per message. See "ASI" directory for documentation.
The `_data_processor` method is calling the parser and processing the data.


## Test Source


The included test source (`test_source.py`) provides an interface
for the user to modify, start, and stop the message stream:
   - `cps <value>` - Set count rate of pixel events (per second)
   - `tdc <value>` - Set TDC Frequency (Hz)
   - `start <duration>` - Send events for duration (seconds)
   - `stop` - Stop sending events
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
- **Threading**: Currently handles one source at a time; can be extended for multiple concurrent sources
- **Memory Usage**: Monitor memory usage for long-running servers

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
