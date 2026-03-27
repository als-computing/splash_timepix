# Implementation Overview: Start/Stop Messages for splash_timepix

## Executive Summary

We successfully implemented start/stop message functionality for the splash_timepix data acquisition system, following the same architectural pattern used in ArroyoXPS. This enables downstream consumers to track the complete lifecycle of data acquisition runs, from initialization through data collection to completion.

## Objective

Add start and stop control messages to the existing ZMQ data stream, allowing downstream processors to:
- Know when an acquisition run begins (start message with configuration)
- Track data flushes during acquisition (event messages)
- Know when an acquisition run ends (stop message with statistics)

This pattern matches the architecture used in ArroyoXPS, making it easier to build unified processing pipelines.

## Approach

We used ArroyoXPS as a reference implementation, studying its:
- Message schema definitions (`schemas.py`)
- ZMQ listener pattern (`XPSLabviewZMQListener`)
- Operator pattern for message processing

We then adapted this pattern for splash_timepix's TimePix3 data structures and message formats.

## Implementation Details

### 1. Message Schemas (`src/splash_timepix/schemas.py`) - NEW

Created Pydantic-based schema definitions for three message types:

**TimePixStart**
- Published when first data arrives
- Contains all acquisition configuration parameters
- Fields: scan_name, tdc_frequency, detector_size, timing parameters, etc.

**TimePixEvent**
- Published for each data flush
- Contains array data (numpy) and flush metadata
- Fields: array, flush_number, cycles_in_flush, statistics, etc.

**TimePixStop**
- Published when acquisition ends (client disconnect or server shutdown)
- Contains run summary statistics
- Fields: scan_name, total_flushes, total_cycles, duration, discarded pixels, etc.

**Benefits:**
- Type safety and validation via Pydantic
- Clear documentation of message structure
- Easy serialization/deserialization

### 2. ZMQ Worker Updates (`src/splash_timepix/workers.py`) - MODIFIED

**Changes:**
- Added `message_queue` parameter to `zmq_worker()` function
- Added logic to publish start/stop control messages from queue
- Added `msg_type="event"` to data flush messages for identification
- Control messages (start/stop) are single-part; data messages are multi-part

**Key Implementation:**
- Control messages have higher priority and are processed before data
- Uses non-blocking sends to avoid blocking on slow subscribers
- Handles ZMQ slow joiner problem with appropriate delays

### 3. Main Application Updates (`src/splash_timepix/app.py`) - MODIFIED

**Changes:**
- Added imports: `uuid`, `datetime`, `TimePixStart`, `TimePixStop`
- Created `message_queue` for control messages (only when using ZMQ worker)
- Generated unique `scan_name` for each acquisition run
- Added tracking variables: `start_message_sent`, `acquisition_start_time`

**Start Message Logic:**
- Sent in `data_callback_np()` when first data packet arrives
- Contains all configuration parameters from app initialization
- Queued to `message_queue` for ZMQ worker to publish

**Stop Message Logic:**
- Sent when client disconnects (detected via `was_client_connected` flag)
- Also sent in `finally` block on server shutdown (if not already sent)
- Contains accumulated statistics: flushes, cycles, packets, duration, discarded pixels

**Key Features:**
- Automatic lifecycle tracking
- No manual intervention required
- Works with both `--exit-on-disconnect` and normal modes

### 4. ZMQ Listener (`src/splash_timepix/listener.py`) - NEW

Created `SplashTimePixZMQListener` class following the ArroyoXPS pattern:

**Functionality:**
- Subscribes to ZMQ PUB socket from splash_timepix
- Receives and parses messages (start/stop/event)
- Converts raw messages to schema objects (TimePixStart, TimePixStop, TimePixEvent)
- Calls operator callback function for each message
- Handles multi-part messages correctly (metadata + array bytes)

**Usage Pattern:**
```python
def my_operator(message):
    if isinstance(message, TimePixStart):
        # Initialize processing
    elif isinstance(message, TimePixEvent):
        # Process data array
    elif isinstance(message, TimePixStop):
        # Finalize processing

listener = SplashTimePixZMQListener(
    zmq_address="tcp://localhost:5657",
    operator=my_operator
)
listener.start()
```

**Benefits:**
- Enables operator pattern for downstream processing
- Type-safe message handling
- Consistent with ArroyoXPS architecture

### 5. Example Files - NEW/MODIFIED

**`src/splash_timepix/example_listener.py`** - NEW
- Demonstrates listener + operator pattern
- Shows how to process all three message types
- Example operator class with start/event/stop handlers

**`src/splash_timepix/example_zmq_sub.py`** - MODIFIED
- Updated to handle start/stop control messages
- Distinguishes single-part (control) vs multi-part (data) messages
- Displays formatted output for all message types

### 6. Testing Infrastructure - NEW

**`tests/test_start_stop_messages.py`** - NEW
- Unit tests for schema validation
- Integration tests for full message flow
- Tests start/event/stop message reception

**`tests/test_start_stop_quick.py`** - NEW
- Quick manual test script
- Subscribes to ZMQ and displays all messages
- Useful for debugging and verification

### 7. Documentation Updates

**`README.md`** - MODIFIED
- Added pydantic to dependencies
- Updated ZMQ message format section with all three message types
- Added "Using the Listener Pattern" section
- Updated architecture diagram
- Added test commands

**`pyproject.toml`** - MODIFIED
- Added pydantic to dependencies

## Message Flow

```
1. Server starts → waits for client connection
2. Client connects → server ready
3. First data arrives → START message sent
   - Contains: scan_name, configuration parameters
4. Data flushes → EVENT messages sent (each flush)
   - Contains: array data + metadata
5. Client disconnects → STOP message sent
   - Contains: scan_name, statistics
6. Server shutdown → cleanup
```

## Technical Challenges Solved

### 1. ZMQ Slow Joiner Problem
**Issue:** Subscribers connecting after messages are sent miss those messages.

**Solution:**
- Added 2-second delay after server start for subscribers to connect
- Added 2-second delay in test scripts after connecting
- Increased ZMQ worker startup delay to 1 second

### 2. Stop Message Timing
**Issue:** Stop message only sent on server shutdown, not when client disconnects.

**Solution:**
- Added client disconnect detection via `was_client_connected` flag
- Send stop message immediately when client disconnects
- Also send in `finally` block as fallback
- Use flag to prevent duplicate stop messages

### 3. Message Type Identification
**Issue:** Need to distinguish control messages from data messages.

**Solution:**
- Control messages (start/stop): single-part (metadata only)
- Data messages (event): multi-part (metadata + array bytes)
- Added `msg_type` field to all messages
- Subscribers check message type and handle accordingly

## Testing

### Unit Tests
- Schema validation tests (all three message types)
- Import tests
- All tests passing

### Integration Tests
- Full message flow: start → events → stop
- Message format validation
- Listener pattern verification

### Manual Testing
- Verified start message on first data
- Verified event messages for each flush
- Verified stop message on client disconnect
- All message types received correctly

## Files Summary

### Created Files (5)
1. `src/splash_timepix/schemas.py` - Message schemas
2. `src/splash_timepix/listener.py` - ZMQ listener class
3. `src/splash_timepix/example_listener.py` - Listener example
4. `tests/test_start_stop_messages.py` - pytest tests
5. `tests/test_start_stop_quick.py` - Quick manual test

### Modified Files (4)
1. `src/splash_timepix/app.py` - Added start/stop message sending
2. `src/splash_timepix/workers.py` - Added message queue support
3. `src/splash_timepix/example_zmq_sub.py` - Updated for start/stop
4. `pyproject.toml` - Added pydantic dependency
5. `README.md` - Updated documentation

## Benefits

1. **Lifecycle Tracking**: Downstream processors can track complete acquisition runs
2. **Type Safety**: Pydantic schemas provide validation and type hints
3. **Consistency**: Matches ArroyoXPS pattern for unified architecture
4. **Backward Compatible**: Existing subscribers continue to work
5. **Operator Pattern**: Enables structured message processing
6. **Better Debugging**: Clear start/stop boundaries for troubleshooting

## Architecture Alignment

The implementation follows the same pattern as ArroyoXPS:

```
ArroyoXPS:                    splash_timepix:
LabVIEW → ZMQ PUB            app.py → ZMQ PUB
    ↓                              ↓
XPSLabviewZMQListener    SplashTimePixZMQListener
    ↓                              ↓
XPSOperator                Your Operator
    ↓                              ↓
Publishers                  Downstream Processing
```

This makes it easier to:
- Build unified processing pipelines
- Share code between systems
- Train users on consistent patterns
- Maintain both systems

## Future Enhancements

Potential improvements:
- Add message versioning for schema evolution
- Add message filtering/topics for selective subscription
- Add message replay capability
- Add metrics/telemetry in messages
- Add support for multiple concurrent acquisitions

## Conclusion

The start/stop message implementation successfully adds lifecycle tracking to splash_timepix while maintaining backward compatibility and following established patterns from ArroyoXPS. The system is production-ready and fully tested.
