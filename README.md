# splash_timepix

Time-resolved TimePix3 detector data streaming and pre-processing system.

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
Multi-threaded TCP server that receives and parses 12-byte TimePix3 packets:
- **Thread 1**: Socket listener (network I/O)
- **Thread 2**: Packet parser (delivers to callback)

See [SOCKET_SERVER_README.md](SOCKET_SERVER_README.md) for details.

### 2. Main Application (`app.py`)
Main application implementing time-resolved binning:
- Receives packets via callback from socket server
- Bins pixel events into 3D arrays (x, y, time) based on TDC triggers
- Flushes accumulated arrays to processing queue
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

### 4. Test Source (`test_source.py`)
Simulated TimePix3 data source for testing:
- Generates realistic packet streams
- Configurable pixel count rate and TDC frequency
- Interactive CLI for control

## Quick Start

### Basic Usage (ZMQ Publishing)

**Terminal 1** - Start server in production mode:
```bash
python -m splash_timepix.app
```

**Terminal 2** - Start data source:
```bash
# Using simulator
python -m splash_timepix.test_source
# Then type:
cps 10000
tdc 1.0
start 120

# OR using real detector
./ASI/live-cli_alpha-1/live-cli
```

**Terminal 3** - Subscribe to published data:
```bash
python test_zmq_subscriber.py
```

### Development Mode (Live Plotting)

**Terminal 1** - Start server with visualization and verbose output:
```bash
python -m splash_timepix.app --plot --verbose
```

**Terminal 2** - Start simulator with low rate:
```bash
python -m splash_timepix.test_source
# Then type:
cps 1000
tdc 0.5
start 300
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
- `--port INT`: Socket server port matching live-cli client port (default: 9090)
- `--buffer-size INT`: Internal message queue size (default: 1000)
- `--zmq-port INT`: ZMQ publishing port (default: 5555)

**Time-Resolved Binning Options:**
- `--tdc-ch INT`: TDC channel to use - 0=both, 1=ch1, 2=ch2 (default: 1)
- `--tdc-edge STR`: TDC edge to trigger on - "rising" or "falling" (default: rising)
- `--tdc-frequency` FLOAT: Expected TDC trigger frequency in Hz (default: 1.0)
- `--t-delta-ns FLOAT`: Time bin width in nanoseconds (default: 10)
- `--flush-interval FLOAT`: Time between 3D x, y, t array flushes in seconds (default: 2.0)

**Display Options:**
- `--stats-update-time INT`: Stats refresh interval in seconds (default: 1)

### `test_source.py` Interactive Commands

After starting the test source, use these commands:

- `cps <value>` - Set pixel count rate (events/second)
- `tdc <value>` - Set TDC frequency (Hz)
- `start <duration>` - Start streaming for duration (seconds)
- `quit` - Exit

### Interactive Commands During Runtime

While `app.py` is running:

- Press **`r`** - Reset session statistics
- Press **`p`** - Print current timing configuration
- Press **Ctrl+C** - Shutdown server
- Press **`q`** (in plot window) - Close visualization

If you see Qt warnings after shutdown, press **ENTER** to clear them.

## Usage Examples

### Example 1: High-Rate Production with ZMQ
```bash
# Production settings for high count rates
python -m splash_timepix.app \
    --tdc-ch 1 \
    --tdc-edge rising \
    --tdc-frequency 1E6 \
    --t-delta-ns 10
```

### Example 2: Low-Rate Development using "test_source"
```bash
# Terminal 1
python -m splash_timepix.app --verbose

# Terminal 2
python -m splash_timepix.test_source
```

### Example 3: Custom Ports
```bash
# Non-default ports for incoming packets and outgoing x, y, t arrays 
python -m splash_timepix.app \
    --port 8080 \
    --zmq-port 6666
```

### Example 4: Replay Recorded Data using Plotting (instead of ZMQ)
```bash
# Terminal 1
python -m splash_timepix.app --plot

# Terminal 2 - replay from file
./ASI/live-cli_alpha-1/live-cli --source-files path/to/recording.tpx3
```

## ZMQ Data Format

Published arrays use a **multi-part message** format:

**Part 1: Metadata (msgpack)**
```python
{
    'shape': (256, 256, 100),     # Array dimensions
    'dtype': 'uint32',             # Numpy dtype
    'timestamp': 1699999999.123,   # Unix timestamp
    'array_count': 42              # Sequential counter
}
```

**Part 2: Array Data (raw bytes)**
- Raw numpy array bytes
- Reconstruct with `np.frombuffer(bytes, dtype).reshape(shape)`

### Example Subscriber
```python
import zmq
import msgpack
import numpy as np

context = zmq.Context()
socket = context.socket(zmq.SUB)
socket.connect("tcp://localhost:5555")
socket.setsockopt(zmq.SUBSCRIBE, b"")

while True:
    # Receive multi-part message
    metadata_bytes = socket.recv()
    array_bytes = socket.recv()
    
    # Unpack
    metadata = msgpack.unpackb(metadata_bytes)
    array = np.frombuffer(array_bytes, dtype=metadata['dtype']).reshape(metadata['shape'])
    
    # Process
    print(f"Received array: {array.shape}, total counts: {np.sum(array)}")
```

## Statistics Display

The application displays real-time statistics:

### Overall Stats
- Server uptime
- Physical memory usage (RSS)
- Total packet count and rate
- Unknown packet count

### Queue Statistics
- **Message queue**: Raw 12-byte packets (socket → parser)
- **Typed packets queue**: Parsed packets (parser → callback)
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

### Simulator (`test_source.py`)
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
pytest
```

### Run Pre-commit Checks
```bash
pre-commit run --all-files
```

### Architecture Overview
```
Data Source → Socket Server → Callback (Binning) → Processing Queue → Worker
                ↓                    ↓                                    ↓
           Parser (12B)        3D Array (x,y,t)               Plot or Publish
```

**Threading:**
1. Socket Listener Thread (I/O bound)
2. Data Processor Thread (CPU bound)
3. Plotting/ZMQ Worker Thread (output bound)
4. Input Listener Thread (user commands)

See [SOCKET_SERVER_README.md](SOCKET_SERVER_README.md) for server details.

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
