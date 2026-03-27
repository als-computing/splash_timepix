# splash_timepix

Streaming and pre-processing time-resolved TimePix3 detector data

## Overview

This package provides a pipeline for streaming, pre-processing, and visualizing data from Amsterdam Scientific Instruments (ASI) TimePix3 detectors. It implements time-resolved spectroscopy binning with TDC (Time-to-Digital Converter) triggering and supports both real-time visualization and ZMQ publishing for downstream analysis.

## Platform Requirements

- **Python 3.9+**
- **Linux/Ubuntu recommended** (input handling uses `select.select()` which is Unix-specific)
- TimePix3 detector with ASI live-cli software

## Installation

Clone the repository and install dependencies:
```bash
git clone https://github.com/als-computing/splash_timepix.git
cd splash_timepix
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

### Dependencies

- `numpy` - Array operations
- `opencv-python` - Real-time visualization
- `pyzmq` - ZMQ publishing
- `msgpack` - Serialization for ZMQ
- `typer` - CLI interface
- `psutil` - System monitoring
- `pydantic` - Message schema validation

## Configuration

- All tool and code style configurations are in `pyproject.toml`
- Pre-commit hooks are configured in `.pre-commit-config.yaml`
- To enable pre-commit hooks (recommended):
```bash
pre-commit install
```

## Architecture

The system consists of four main components:

### 1. Socket Server (`socket_server.py`)
Multi-threaded TCP server that receives 12-byte TimePix3 packets and parses them with NumPy (`parser`):
- **Thread 1**: Socket listener (reads TCP chunks, batches complete packets into raw byte buffers)
- **Thread 2**: Vectorized parser (`parse_batch`) → callback receives a `BatchParseResult` (arrays per packet type)

See [info/SOCKET_SERVER_README.md](info/SOCKET_SERVER_README.md) for details.

### 2. Main Application (`app.py`)
Main application implementing time-resolved binning:
- Receives packets via callback from socket server
- Bins pixel events into 3D arrays (x, y, time) based on TDC triggers
- Flushes accumulated 3D arrays to processing queue
- Provides statistics display and user commands

### 3. Worker Threads
Two alternative workers for consuming 3D arrays:

**Plotting Worker** (`--plot` flag):
- Real-time OpenCV visualization
- Displays 2D heatmap (x vs time, summed over y)
- Shows statistics overlay
- Interactive (press 'q' to close)

**ZMQ Worker** (default):
- Publishes arrays via ZMQ PUB socket
- Uses msgpack serialization
- Multi-part messages (metadata + array bytes)
- Supports multiple subscribers
- Publishes start/stop control messages for acquisition lifecycle tracking

### 4. Simulated Source (`simulator_cli.py`)
Simulated TimePix3 data source for testing:
- Generates realistic packet streams
- Configurable pixel count rate and TDC frequency
- Interactive CLI for control

## Quick Start - Basic Usage Examples

The package installs **`tpx-stream`** as the console entry point for the streaming app (see `pyproject.toml`); `python -m splash_timepix.app` is equivalent.

### Production Mode (ZMQ Publishing)

**Terminal 1** - Start server:
```bash
tpx-stream
# OR via
python -m splash_timepix.app
```

**Terminal 2** - Subscribe to published data:
```bash
# Use (or modify) the example
python -m splash_timepix.example_zmq_sub
# OR connect with your application
```

**Terminal 3** - Start detector:
```bash
./ASI/live-cli_alpha-1/live-cli
```

### Display Real-time Data (Live Plotting)

**Terminal 1** - Start server with visualization:
```bash
tpx-stream --plot
```

**Terminal 2** - Start data source:
```bash
# Using real detector
./ASI/live-cli_alpha-1/live-cli
# OR replaying from file
./ASI/live-cli_alpha-1/live-cli --source-files path/to/recording.tpx3
# OR using the simulator
python -m splash_timepix.simulator_cli
```

### Development Mode (Debugging)

**Terminal 1** - Start server with verbose output
```bash
tpx-stream --verbose
```

**Terminal 2**
```bash
# (I) Low Count Rates (Simulator)
python -m splash_timepix.simulator_cli
cps 3
tdc 1
start 60
# OR (II) High Count Rates [<100kcps] (Simulator)
python -m splash_timepix.simulator_cli
cps 100000
tdc 0.1
start 60
# OR (III) Using Replay From File (live-cli)
./ASI/live-cli_alpha-1/live-cli --source-files path/to/recording.tpx3
```

## Command-Line Options

### `app.py` Options
```bash
python -m splash_timepix.app [OPTIONS]
```

**Flags:**
- `--plot`: Enable real-time plotting (default: ZMQ publishing)
- `--verbose`: Show detailed logs and packet samples (default: warnings only)

**Server Options:**
- `--host STR`: Host for server to bind to (default: "localhost")
- `--port INT`: Socket server port matching live-cli client port (default: 9090)
- `--buffer-size INT`: Internal message queue size (default: 1000)
- `--callback-batch-size INT`: Number of parsed packets to batch per callback (default: 10000)
- `--zmq-port INT`: ZMQ publishing port (default: 5657)

**Time-Resolved Binning Options:**
- `--tdc-ch INT`: TDC channel to use - 0=both, 1=ch1, 2=ch2 (default: 1)
- `--tdc-edge STR`: TDC edge to trigger on - "rising" or "falling" (default: rising)
- `--tdc-frequency` FLOAT: Expected TDC trigger frequency in Hz (default: 1.0)
- `--t-delta-ns FLOAT`: Time bin width in nanoseconds (default: 10)
- `--flush-interval FLOAT`: Time between 3D x, y, t array flushes in seconds (default: 2.0)

**Display Options:**
- `--stats-update-time INT`: Stats refresh interval in seconds (default: 1)

### `simulator_cli.py` Interactive Commands

After starting the test source, use these commands:

- `cps <value>` - Set pixel count rate (events/second)
- `tdc <value>` - Set TDC frequency (Hz)
- `start <duration>` - Start streaming for duration (seconds)
- `stop` - Stop streaming data
- `quit` - Exit

## ZMQ Message Format

The system publishes three types of messages via ZMQ:

### 1. Start Message (Single-part)
Published when data acquisition begins (first data arrives):
```python
{
    'msg_type': 'start',
    'scan_name': 'acquisition_20250112T160536Z_8b850728',
    'tdc_frequency_hz': 10.0,
    'detector_size_x': 256,
    'detector_size_y': 256,
    'n_bins': 350,
    't_delta_ns': 285714.29,
    # ... other configuration parameters
}
```

### 2. Event Message (Multi-part)
Published for each data flush:

**Part 1: Metadata (msgpack)**
```python
{
    'msg_type': 'event',
    'shape': (256, 256, 350),     # Array dimensions
    'dtype': 'uint32',             # Numpy dtype
    'timestamp': 1699999999.123,   # Unix timestamp
    'flush_number': 1,             # Sequential flush number
    'cycles_in_flush': 10,         # TDC cycles in this flush
    'total_cycles': 10,            # Cumulative cycle count
    # ... other metadata
}
```

**Part 2: Array Data (raw bytes)**
- Raw numpy array bytes
- Reconstruct with `np.frombuffer(bytes, dtype).reshape(shape)`

### 3. Stop Message (Single-part)
Published when data acquisition ends (client disconnects or server shuts down):
```python
{
    'msg_type': 'stop',
    'scan_name': 'acquisition_20250112T160536Z_8b850728',
    'total_flushes': 9,
    'total_cycles': 99,
    'total_packets': 50000,
    'acquisition_duration_s': 28.91,
    # ... statistics
}
```

### Example Subscriber

**See also `example_zmq_sub.py`** for a complete example that handles all message types.

```python
import zmq
import msgpack
import numpy as np

context = zmq.Context()
socket = context.socket(zmq.SUB)
socket.connect("tcp://localhost:5657")
socket.setsockopt(zmq.SUBSCRIBE, b"")

while True:
    # Receive first part (metadata)
    metadata_bytes = socket.recv()
    metadata = msgpack.unpackb(metadata_bytes)
    msg_type = metadata.get('msg_type')

    if msg_type == 'start':
        print(f"Acquisition started: {metadata['scan_name']}")
    elif msg_type == 'stop':
        print(f"Acquisition stopped: {metadata['scan_name']}")
    elif msg_type == 'event' or msg_type is None:
        # Event message - receive array data
        array_bytes = socket.recv()
        array = np.frombuffer(array_bytes, dtype=metadata['dtype']).reshape(metadata['shape'])
        print(f"Received flush #{metadata.get('flush_number')}: {array.shape}")
```

### Using the Listener Pattern

For a more structured approach, use `SplashTimePixZMQListener` (similar to ArroyoXPS):

**See also `example_listener.py`**

```python
from splash_timepix.listener import SplashTimePixZMQListener
from splash_timepix.schemas import TimePixStart, TimePixEvent, TimePixStop

def my_operator(message):
    if isinstance(message, TimePixStart):
        # Initialize processing
        print(f"Start: {message.scan_name}")
    elif isinstance(message, TimePixEvent):
        # Process data array
        print(f"Event: flush #{message.flush_number}, shape={message.array.shape}")
    elif isinstance(message, TimePixStop):
        # Finalize processing
        print(f"Stop: {message.scan_name}, {message.total_flushes} flushes")

listener = SplashTimePixZMQListener(
    zmq_address="tcp://localhost:5657",
    operator=my_operator
)
listener.start()  # Blocks until stopped
```

## Statistics Display

The application displays real-time statistics:

### Overall Stats
- Server uptime
- Physical memory usage (RSS)
- Total packet count and rate
- Unknown packet count

### Queue Statistics
- **Message queue**: Raw byte batches (socket reader → vectorized parser)
- **Callback**: One `BatchParseResult` per batch (pixel/TDC/control arrays), not a list of packet objects
- **x, y, t array queue**: 3D arrays (callback → worker)
- **Single array size**: Memory per 3D array

### Session Stats
- Duration since last reset
- Packet count and rate for current session
- Reset with **`r`** key

### Verbose Mode (`--verbose`)
- Last 10 valid packets
- Recent error messages

## Time-Resolved Binning Explained

The application bins pixel events into 3D arrays based on TDC triggers:

1. **TDC trigger** arrives → sets time zero (`t_zero`)
2. **Pixel events** are binned relative to `t_zero`:
   - Calculate time bin: `bin = (pixel_time - t_zero) / t_delta`
   - Increment: `array[x, y, bin] += 1`
3. After **flush_interval** time, the array is flushed to the worker
4. Worker either **plots** or **publishes** the array

**Key parameters:**
- `t_delta`: Width of each time bin (temporal resolution)
- `tdc_frequency`: Inverse of total time window to capture per TDC trigger
- `n_bins`: Automatically calculated as `ceil((1 / tdc_frequency) / t_delta)`

**Pixels outside the time window** or **before the first TDC** are discarded and counted for diagnostics.

## Data Sources

### Simulator/ Test Source (`simulator_cli.py`)
- Generates Poisson-distributed pixel events
- Realistic TDC pulses with configurable frequency
- Useful for testing and development

### Live Detector (`live-cli`)
Real-time streaming from TimePix3:
```bash
./ASI/live-cli_alpha-1/live-cli
```

### Recorded Data (`live-cli --source-files`)
Replay recorded `.tpx3` files:
```bash
./ASI/live-cli_alpha-1/live-cli --source-files path/to/file.tpx3
```

## Development

### Run Tests
```bash
# Run all tests
pytest

# Run start/stop message tests
pytest tests/test_start_stop_messages.py -v

# Run quick manual test (requires server running)
python tests/test_start_stop_quick.py
```

### Run Pre-commit Checks
```bash
pre-commit run --all-files
```

### Architecture Overview
```
Data Source → Socket Server → Callback (Binning) → Processing Queue → Worker
                ↓                    ↓                                    ↓
      Parser (NumPy batches)   3D Array (x,y,t)               Plot or Publish
                                                                    ↓
                                                          ZMQ PUB (start/event/stop)
                                                                    ↓
                                                          SplashTimePixZMQListener
                                                                    ↓
                                                          Operator (your processing)
```

**Threading:**
1. Socket Listener Thread (I/O bound)
2. Data Processor Thread (CPU bound)
3. Plotting/ZMQ Worker Thread (output bound)
4. Input Listener Thread (user commands)

See [info/SOCKET_SERVER_README.md](info/SOCKET_SERVER_README.md) for server details.

## Troubleshooting

### No data appearing in plots/ZMQ
- Check TDC channel matches your trigger source (`--tdc-ch`)
- Check TDC edge setting (`--tdc-edge`)
- Verify you're receiving TDC packets (use `--verbose`)

### High memory usage
- Increase `t_delta` (fewer time bins, smaller 3D array size)
- Reduce `flush_interval` (flush more frequently)
- Increase xyt_queue consumption rate

### Queue overflow warnings
- Increase `--buffer-size`
- Reduce callback processing time
- Increase `--callback_batch_size`
- Check if worker thread is keeping up

### Qt warnings on shutdown
- Press **ENTER** after Ctrl+C to clear buffered warnings
- These are harmless and occur during daemon thread cleanup

### Warnings about pixels outside time window
When using the test simulator, you may see warnings like:
```
WARNING - Pixel outside time window: t_relative=... ps
```
This is **expected behavior**. The simulator uses real-time scheduling while packets
use detector timestamps, which can drift slightly. These warnings indicate the
application is correctly identifying and discarding events that fall outside the
configured time window. In production with real detector hardware, similar edge
cases may occur due to timing jitter, and the application handles them correctly.
