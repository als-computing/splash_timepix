# Sample Start Document / Message

This document shows what a **start message** looks like when published by the splash_timepix server at the beginning of a data acquisition run. The message is sent once when the first data arrives, over ZMQ (single-part, msgpack-encoded). Downstream consumers (e.g. ArroyoXPS) use it to initialize processing for the run.

## Wire format

- **Transport:** ZMQ PUB socket (default port 5657)
- **Encoding:** msgpack (binary)
- **Parts:** Single message part (metadata only; no array data)

## Sample start message (JSON equivalent)

The following is the logical content of the start message as a JSON object. Over the wire it is serialized with msgpack, but the structure is the same.

```json
{
  "msg_type": "start",
  "scan_name": "acquisition_20250128T143022Z_a1b2c3d4",
  "tdc_frequency_hz": 1000.0,
  "t_delta_ns": 10.0,
  "t_cycle_ns": 1000000.0,
  "n_bins": 100,
  "detector_size_x": 256,
  "detector_size_y": 256,
  "flush_interval_s": 1.0,
  "cycles_per_flush": 1000,
  "tdc_channel": 1,
  "tdc_edge": "rising",
  "collapse_y": false,
  "zmq_port": 5657,
  "tcp_port": 9090
}
```

## Field descriptions

| Field | Type | Description |
|-------|------|-------------|
| `msg_type` | string | Always `"start"` for this message type. |
| `scan_name` | string | Unique identifier for this acquisition run. Format: `acquisition_YYYYMMDDTHHMMSSZ_<uuid8>` (UTC, ISO 8601). Example: `acquisition_20250128T143022Z_a1b2c3d4`. |
| `tdc_frequency_hz` | float | TDC trigger frequency in Hz. |
| `t_delta_ns` | float | Time bin width in nanoseconds. |
| `t_cycle_ns` | float | Full time cycle in nanoseconds. |
| `n_bins` | int | Number of time bins. |
| `detector_size_x` | int | Detector X dimension (pixels). |
| `detector_size_y` | int | Detector Y dimension (pixels). |
| `flush_interval_s` | float | Interval between data flushes in seconds. |
| `cycles_per_flush` | int | Expected number of TDC cycles per flush. |
| `tdc_channel` | int | TDC channel: 0 = both, 1 = channel 1, 2 = channel 2. |
| `tdc_edge` | string | TDC edge trigger: `"rising"` or `"falling"`. |
| `collapse_y` | bool | Whether the Y dimension is collapsed in the 3D array. |
| `zmq_port` | int | ZMQ publishing port for this server. |
| `tcp_port` | int | TCP socket port for the live-cli connection. |

## Time in `scan_name`

The timestamp in `scan_name` is **UTC** in **ISO 8601** form: `YYYYMMDDTHHMMSSZ` (e.g. `20250128T143022Z`). It is generated when a new client connects and the run starts, using `datetime.now(timezone.utc)`, so it is unambiguous and sortable across machines and timezones.

## Usage in code

- **splash_timepix:** The message is built from `TimePixStart` (see `src/splash_timepix/schemas.py`) and published via the ZMQ worker after `model_dump()`.
- **Subscribers:** Parse the first (and only) ZMQ frame with msgpack, then read `msg_type == "start"` and use the fields above to configure processing for the run.

## Related message types

- **Event message** (`msg_type: "event"`): Multi-part ZMQ message (metadata + raw array bytes); one per flush.
- **Stop message** (`msg_type: "stop"`): Single-part, msgpack; sent when acquisition ends (disconnect or shutdown).

See `IMPLEMENTATION_OVERVIEW.md` and `schemas.py` for full details.
