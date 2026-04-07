# Testing Guide for Start/Stop Messages

This guide explains how to test the start/stop message implementation in splash_timepix, including integration with the ArroyoXPS listener.

## Prerequisites

1. Install splash_timepix (in its repo):
   ```bash
   cd /path/to/splash_timepix
   pip install -e .[dev]
   ```

2. For ArroyoXPS integration: create and use the `arroyoxps` conda environment and install ArroyoXPS there (see ArroyoXPS docs).

3. Simulator is included for testing without hardware.

---

## How to Run (splash_timepix + ArroyoXPS)

Use **three terminals**. Start in this order: server first, then listener, then simulator.

### Terminal 1: Start splash_timepix server

```bash
# Go to splash_timepix project
cd /home/gabrielgazolla/Downloads/task/splash_timepix

# Activate your splash_timepix environment (or base if installed there)
# conda activate splash_timepix   # if you use one

python -m splash_timepix.app --tdc-frequency 10 --flush-interval 1.0
```

Leave this running. You should see the server listening (e.g. on port 9090 for TCP, 5657 for ZMQ).

### Terminal 2: Start ArroyoXPS listener

```bash
# Activate the arroyoxps conda environment (required)
conda activate arroyoxps

# Go to ArroyoXPS project
cd /home/gabrielgazolla/Downloads/task/ArroyoXPS

# Run the TimePix ZMQ listener (DummyOperator prints start/event/stop)
python -m tr_ap_xps.timepix
```

Leave this running. It will connect to `tcp://localhost:5657` (splash_timepix’s ZMQ port).

### Terminal 3: Start splash_timepix simulator

```bash
# Go to splash_timepix project
cd /home/gabrielgazolla/Downloads/task/splash_timepix

# Same environment as Terminal 1
python -m splash_timepix.simulator_cli
```

Then in the simulator CLI:

```
cps 1000
tdc 10
start 10
```

This sends data for 10 seconds. Type `stop` or wait for the run to finish.

### What to expect

- **splash_timepix server (Terminal 1):** Logs when start/stop are queued and when flushes are published.
- **ArroyoXPS listener (Terminal 2):** Connects to `tcp://localhost:5657`, receives and logs:
  - **Start:** e.g. `Dummy operator received START: scan_name=acquisition_YYYYMMDDTHHMMSSZ_xxxxxxxx` (UTC, ISO 8601)
  - **Events:** e.g. `Dummy operator received EVENT with image shape: (256, 256, 350)` (shape may vary)
  - **Stop:** e.g. `Dummy operator received STOP`
- **Simulator (Terminal 3):** Sends packets to the server; `start 10` runs for 10 seconds.

If you see start, multiple events, and stop in the ArroyoXPS terminal, the pipeline is working.

---

## Troubleshooting

### Error: `'XPSOperator' object has no attribute 'recv'`

This usually means the listener is running in the wrong environment or from the wrong directory.

- **Use the ArroyoXPS conda environment:**
  `conda activate arroyoxps`
  The `arroyopy` package (and correct `ZMQListener` signature) must be available in this env.

- **Run from the ArroyoXPS project directory:**
  `cd /home/gabrielgazolla/Downloads/task/ArroyoXPS`
  Then run `python -m tr_ap_xps.timepix` so that `tr_ap_xps` and its config (e.g. ZMQ port) resolve correctly.

- **Check ArroyoXPS code:** In `tr_ap_xps.timepix`, `XPSTimepixZMQListener` must call `super().__init__(operator, zmq_socket)` (operator first, socket second) to match `arroyopy.zmq.ZMQListener`. If you still see the error after fixing env/cd, verify that `__init__` passes arguments in that order.

### No messages received

- Confirm the server is running: `ps aux | grep splash_timepix`
- ZMQ default port is **5657**; ArroyoXPS must use the same (see ArroyoXPS `settings.yaml` / config).
- Start order: server → listener → simulator. If the subscriber connects after the server has already published the start message, you may miss start (ZMQ “slow joiner”); reconnect and trigger a new run.

### Start message not sent

- Ensure data is actually reaching the server (e.g. simulator connected and `start` issued).
- Run server with `--verbose` to see when start is queued.

### Stop message not sent

- Stop is sent on server shutdown (Ctrl+C) or when the client (simulator) disconnects.
- Check server logs for errors during shutdown.

### Listener not receiving messages

- ZMQ address must match server port (default `tcp://localhost:5657`).
- Subscriber should subscribe to all: `socket.setsockopt(zmq.SUBSCRIBE, b"")`.
- After starting the listener, wait a second before starting the simulator to reduce slow-joiner effects.

---

## Other Testing Methods

### Method 1: splash_timepix only (no ArroyoXPS)

#### Terminal 1: Server

```bash
cd /path/to/splash_timepix
python -m splash_timepix.app --tdc-frequency 10 --flush-interval 2.0 --verbose
```

#### Terminal 2: Simulator

```bash
cd /path/to/splash_timepix
python -m splash_timepix.simulator_cli
# In CLI: cps 1000, tdc 10, start 30
```

#### Terminal 3: Example ZMQ subscriber

```bash
cd /path/to/splash_timepix
python -m splash_timepix.example_zmq_sub
```

**Expected:** START with config, multiple EVENTs (flushes), STOP when run ends.

#### Terminal 3 (alternative): Example listener

```bash
cd /path/to/splash_timepix
python -m splash_timepix.example_listener
```

**Expected:** Operator processes START, EVENTs, and STOP.

### Method 2: Automated integration test

```bash
cd /path/to/splash_timepix
python tests/test_start_stop_messages.py
```

This starts server and simulator in subprocesses, subscribes to ZMQ, and checks that start/event/stop messages are received.

### Method 3: Unit tests (pytest)

```bash
cd /path/to/splash_timepix
pytest tests/test_schemas.py -v
pytest tests/test_listener.py -v
```

### Method 4: Real hardware (TimePix3)

1. Start server: `cd /path/to/splash_timepix` then `python -m splash_timepix.app --tdc-frequency 1000`
2. Connect live-cli in another terminal.
3. Start acquisition from live-cli (or your site workflow).
4. Monitor: `python -m splash_timepix.example_zmq_sub`

---

## What to verify

### Start message

- [ ] `msg_type: "start"`
- [ ] Has `scan_name`
- [ ] Has config (tdc_frequency, detector_size, etc.)
- [ ] Sent when first data arrives

### Event messages

- [ ] `msg_type: "event"` (or compatible)
- [ ] Multi-part: metadata + array bytes
- [ ] Flush metadata (flush_number, shape, etc.)

### Stop message

- [ ] `msg_type: "stop"`
- [ ] Same `scan_name` as start
- [ ] Stats: total_flushes, total_cycles, duration
- [ ] Sent on shutdown or client disconnect

### Listener (splash_timepix or ArroyoXPS)

- [ ] Subscribes to ZMQ (port 5657)
- [ ] Converts to schema objects
- [ ] Handles start, event, stop in order

---

## Debug mode

Verbose server logging:

```bash
cd /path/to/splash_timepix
python -m splash_timepix.app --verbose
```

Shows when start/stop are queued and when ZMQ messages are published.
