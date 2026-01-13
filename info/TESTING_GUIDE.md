# Testing Guide for Start/Stop Messages

This guide explains how to test the start/stop message implementation in splash_timepix.

## Prerequisites

1. Install dependencies:
```bash
pip install -e .[dev]
```

2. Make sure you have the simulator available (for testing without hardware)

## Testing Methods

### Method 1: Manual Testing with Simulator (Recommended)

This is the easiest way to test the full flow.

#### Terminal 1: Start the streaming server
```bash
python -m splash_timepix.app \
    --tdc-frequency 10 \
    --flush-interval 2.0 \
    --verbose
```

#### Terminal 2: Start the simulator
```bash
python -m splash_timepix.simulator_cli
# Then in the simulator:
cps 1000
tdc 10
start 30  # Run for 30 seconds
```

#### Terminal 3: Test with example subscriber
```bash
python -m splash_timepix.example_zmq_sub
```

**Expected output:**
- You should see a START message with all configuration details
- Multiple EVENT messages (flushes) with array data
- A STOP message when you stop the simulator or server

#### Terminal 3 (Alternative): Test with listener
```bash
python -m splash_timepix.example_listener
```

**Expected output:**
- Operator processes START message
- Operator processes multiple EVENT messages
- Operator processes STOP message with statistics

### Method 2: Quick Integration Test Script

Run the automated test script:

```bash
python tests/test_start_stop_messages.py
```

This script:
- Starts the server in a subprocess
- Starts the simulator
- Subscribes to messages
- Verifies start/stop/event messages are received
- Cleans up

### Method 3: Unit Tests (pytest)

Run unit tests for schemas and listener:

```bash
pytest tests/test_schemas.py -v
pytest tests/test_listener.py -v
```

### Method 4: Manual Testing with Real Hardware

If you have TimePix3 hardware:

1. Start the server:
```bash
python -m splash_timepix.app --tdc-frequency 1000
```

2. Start live-cli (in another terminal)

3. Start acquisition:
```bash
tpx-acq -tdc 1000 -t 60
```

4. Monitor messages:
```bash
python -m splash_timepix.example_zmq_sub
```

## What to Verify

### Start Message
- [ ] Message has `msg_type: "start"`
- [ ] Contains scan_name
- [ ] Contains all configuration parameters (tdc_frequency, detector_size, etc.)
- [ ] Sent when first data arrives

### Event Messages
- [ ] Messages have `msg_type: "event"` (or no msg_type for backward compatibility)
- [ ] Multi-part messages (metadata + array bytes)
- [ ] Contains array data
- [ ] Contains flush metadata (flush_number, cycles_in_flush, etc.)

### Stop Message
- [ ] Message has `msg_type: "stop"`
- [ ] Contains scan_name (matches start message)
- [ ] Contains statistics (total_flushes, total_cycles, duration, etc.)
- [ ] Sent on shutdown

### Listener
- [ ] Successfully subscribes to ZMQ
- [ ] Converts messages to schema objects
- [ ] Calls operator callback for each message type
- [ ] Handles multi-part messages correctly

## Troubleshooting

### No messages received
- Check that server is running: `ps aux | grep splash_timepix`
- Check ZMQ port: default is 5657
- Verify simulator/server is sending data

### Start message not sent
- Check that data is actually arriving (use `--verbose` flag)
- Verify message_queue is created (only for ZMQ worker, not plotting)

### Stop message not sent
- Check server logs for errors
- Verify server shutdown is graceful (Ctrl+C)

### Listener not receiving messages
- Check ZMQ address matches server port
- Verify subscription: `socket.setsockopt(zmq.SUBSCRIBE, b"")`
- Check for ZMQ slow joiner problem (wait a moment after connecting)

## Debug Mode

Enable verbose logging:

```bash
python -m splash_timepix.app --verbose
```

This shows:
- When start/stop messages are sent
- Message queue operations
- ZMQ publish operations
