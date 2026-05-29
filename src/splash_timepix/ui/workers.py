"""QThread workers for background tasks.

Workers communicate with the UI via Qt signals to ensure thread safety.
"""

import importlib.util
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional, Tuple

import msgpack
import numpy as np
import zmq
from PySide6.QtCore import QObject, QProcess, QThread, Signal

from splash_timepix.serval_client import ServalClient

logger = logging.getLogger(__name__)


# =============================================================================
# Data classes for signal payloads
# =============================================================================


@dataclass
class FlushData:
    """Data payload for a received flush."""

    array: np.ndarray
    metadata: dict


@dataclass
class ServalStatus:
    """Status data from Serval polling."""

    connected: bool
    pixel_event_rate: float = 0.0
    tdc1_event_rate: float = 0.0
    tdc2_event_rate: float = 0.0
    frame_count: int = 0
    elapsed_time: float = 0.0
    time_left: float = 0.0
    status: str = "UNKNOWN"
    error: Optional[str] = None


@dataclass
class HeartbeatStatus:
    """Status data from app.py heartbeat."""

    connected: bool
    state: str = "unknown"
    uptime_s: float = 0.0
    data_port: int = 0
    tcp_port: int = 0
    error: Optional[str] = None
    # (current_size, max_size); None if not present in message (old server / disconnected)
    q_ingest: Optional[Tuple[int, int]] = None
    q_xyt: Optional[Tuple[int, int]] = None
    q_zmq_control: Optional[Tuple[int, int]] = None


def _heartbeat_queue_pair(msg: Dict[str, Any], sz_key: str, mx_key: str) -> Optional[Tuple[int, int]]:
    if sz_key in msg and mx_key in msg:
        return (int(msg[sz_key]), int(msg[mx_key]))
    return None


# =============================================================================
# ZMQ Subscriber Worker
# =============================================================================


class ZmqSubscriberWorker(QThread):
    """Background thread that subscribes to ZMQ data stream from app.py."""

    flush_received = Signal(object)  # FlushData
    connection_changed = Signal(bool)
    error_occurred = Signal(str)

    def __init__(self, port: int = 5657, parent=None):
        super().__init__(parent)
        self.port = port
        self._running = False
        self._connected = False

    def run(self):
        self._running = True
        context = zmq.Context()
        socket = context.socket(zmq.SUB)

        try:
            socket.connect(f"tcp://localhost:{self.port}")
            socket.setsockopt(zmq.SUBSCRIBE, b"")
            socket.setsockopt(zmq.RCVTIMEO, 1000)

            logger.info(f"ZMQ subscriber connected to tcp://localhost:{self.port}")

            while self._running:
                try:
                    # Receive first part (always present)
                    metadata_bytes = socket.recv()
                    metadata = msgpack.unpackb(metadata_bytes)
                    msg_type = metadata.get("msg_type")

                    if not self._connected:
                        self._connected = True
                        self.connection_changed.emit(True)

                    # Control messages (start/stop) are single-part; data (event) messages are multi-part
                    is_data_message = msg_type != "start" and msg_type != "stop"
                    if not is_data_message:
                        logger.info(
                            "ZMQ %s received for scan: %s",
                            msg_type,
                            metadata.get("scan_name", "?"),
                        )
                        continue

                    # Data message: receive second part (array bytes)
                    socket.setsockopt(zmq.RCVTIMEO, 1000)
                    try:
                        array_bytes = socket.recv()
                    except zmq.Again:
                        logger.warning("Expected second part for data message but none received")
                        continue
                    finally:
                        socket.setsockopt(zmq.RCVTIMEO, 1000)

                    shape = tuple(metadata["shape"])
                    dtype = metadata["dtype"]
                    array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)
                    flush_data = FlushData(array=array, metadata=metadata)
                    self.flush_received.emit(flush_data)

                except zmq.Again:
                    continue
                except Exception as e:
                    logger.error(f"Error receiving ZMQ data: {e}")
                    self.error_occurred.emit(str(e))
                    if self._connected:
                        self._connected = False
                        self.connection_changed.emit(False)

        except Exception as e:
            logger.error(f"ZMQ subscriber error: {e}")
            self.error_occurred.emit(str(e))

        finally:
            socket.close()
            context.term()
            if self._connected:
                self._connected = False
                self.connection_changed.emit(False)
            logger.info("ZMQ subscriber stopped")

    def stop(self):
        self._running = False


# =============================================================================
# Heartbeat Monitor Worker
# =============================================================================


class HeartbeatMonitorWorker(QThread):
    """Background thread that monitors app.py heartbeat."""

    status_updated = Signal(object)  # HeartbeatStatus
    connection_changed = Signal(bool)

    def __init__(self, port: int = 5658, parent=None):
        super().__init__(parent)
        self.port = port
        self._running = False
        self._connected = False
        self._last_heartbeat_time = 0.0

    def run(self):
        self._running = True
        context = zmq.Context()
        socket = context.socket(zmq.SUB)

        try:
            socket.connect(f"tcp://localhost:{self.port}")
            socket.setsockopt(zmq.SUBSCRIBE, b"")
            socket.setsockopt(zmq.RCVTIMEO, 2000)

            logger.info(f"Heartbeat monitor connected to tcp://localhost:{self.port}")

            while self._running:
                try:
                    msg_bytes = socket.recv()
                    msg = msgpack.unpackb(msg_bytes)

                    self._last_heartbeat_time = time.time()

                    if not self._connected:
                        self._connected = True
                        self.connection_changed.emit(True)

                    status = HeartbeatStatus(
                        connected=True,
                        state=msg.get("state", "unknown"),
                        uptime_s=msg.get("uptime_s", 0.0),
                        data_port=msg.get("data_port", 0),
                        tcp_port=msg.get("tcp_port", 0),
                        q_ingest=_heartbeat_queue_pair(msg, "q_ingest_sz", "q_ingest_max"),
                        q_xyt=_heartbeat_queue_pair(msg, "q_xyt_sz", "q_xyt_max"),
                        q_zmq_control=_heartbeat_queue_pair(msg, "q_ctrl_sz", "q_ctrl_max"),
                    )
                    self.status_updated.emit(status)

                except zmq.Again:
                    if self._connected and (time.time() - self._last_heartbeat_time > 3.0):
                        self._connected = False
                        self.connection_changed.emit(False)
                        status = HeartbeatStatus(connected=False, error="Heartbeat timeout")
                        self.status_updated.emit(status)
                    continue
                except Exception as e:
                    logger.error(f"Heartbeat monitor error: {e}")
                    if self._connected:
                        self._connected = False
                        self.connection_changed.emit(False)

        except Exception as e:
            logger.error(f"Heartbeat monitor error: {e}")

        finally:
            socket.close()
            context.term()
            logger.info("Heartbeat monitor stopped")

    def stop(self):
        self._running = False


class ServalPollerWorker(QThread):
    """Background thread that polls Serval server for status."""

    status_updated = Signal(object)  # ServalStatus
    connection_changed = Signal(bool)

    def __init__(self, poll_interval: float = 1.0, parent=None):
        super().__init__(parent)
        self.poll_interval = poll_interval
        self._running = False
        self._connected = False

    def run(self):
        self._running = True
        client = ServalClient()

        time.sleep(3.0)  # Give Serval time to start

        while self._running:
            try:
                meta = client.get_measurement_status()

                status = ServalStatus(
                    connected=True,
                    pixel_event_rate=meta.get("PixelEventRate", 0) or 0,
                    tdc1_event_rate=meta.get("Tdc1EventRate", 0) or 0,
                    tdc2_event_rate=meta.get("Tdc2EventRate", 0) or 0,
                    frame_count=meta.get("FrameCount", 0) or 0,
                    elapsed_time=meta.get("ElapsedTime", 0.0) or 0.0,
                    time_left=meta.get("TimeLeft", 0.0) or 0.0,
                    status=meta.get("Status") or "IDLE",
                )

                if not self._connected:
                    self._connected = True
                    self.connection_changed.emit(True)

                self.status_updated.emit(status)

            except Exception as e:
                logger.debug(f"Serval poll error: {e}")
                if self._connected:
                    self._connected = False
                    self.connection_changed.emit(False)
                status = ServalStatus(connected=False, error=str(e))
                self.status_updated.emit(status)

            # Sleep in small increments for faster shutdown
            sleep_remaining = self.poll_interval
            while sleep_remaining > 0 and self._running:
                time.sleep(min(0.1, sleep_remaining))
                sleep_remaining -= 0.1

        logger.info("Serval poller stopped")

    def stop(self):
        self._running = False


# =============================================================================
# Process Manager
# =============================================================================


class ProcessManager(QObject):
    """Manages spawned subprocesses (Serval, app.py, live-cli, acq.py)."""

    process_started = Signal(str)
    process_stopped = Signal(str, int)
    process_output = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.processes: dict[str, QProcess] = {}
        self._project_root = Path(__file__).parent.parent.parent.parent

    def start_serval(self) -> bool:
        serval_dir = self._project_root / "ASI"
        serval_jar = "serval-4.1.1.jar"

        if not (serval_dir / serval_jar).exists():
            logger.error(f"Serval JAR not found: {serval_dir / serval_jar}")
            return False

        return self._start_process(
            name="serval",
            program="java",
            args=["-Xmx8G", "-jar", serval_jar],
            working_dir=serval_dir,
        )

    def start_streaming_server(
        self,
        tdc_frequency: float,
        tdc_channel: int = 1,
        tdc_edge: str = "rising",
        callback_batch_size: int = 10_000,
        n_bins: int = 350,
        collapse_y: bool = True,
        exit_on_disconnect: bool = True,
        alignment: bool = False,
        alignment_rate_hz: float = 30.0,
    ) -> bool:
        args = [
            "-m",
            "splash_timepix.app",
            "--tdc-frequency",
            str(tdc_frequency),
            "--tdc-ch",
            str(tdc_channel),
            "--tdc-edge",
            tdc_edge,
            "--callback-batch-size",
            str(int(callback_batch_size)),
            "--n-bins",
            str(n_bins),
        ]
        if alignment:
            # Alignment mode forces n_bins=1 and ignores collapse_y server-side;
            # we still pass --n-bins above (harmless override below) so the CLI
            # signature stays uniform across modes.
            args += ["--alignment", "--alignment-rate-hz", str(float(alignment_rate_hz))]
        elif collapse_y:
            args.append("--collapse-y")
        if exit_on_disconnect:
            args.append("--exit-on-disconnect")

        return self._start_process(
            name="streaming",
            program=sys.executable,
            args=args,
            working_dir=self._project_root,
        )

    def start_simulator(self, tdc_frequency: float = 1.0, cps: float = 1000.0, duration: int = 60) -> bool:
        """Start the simulator in auto-start mode."""
        args = [
            "-m",
            "splash_timepix.simulator_cli",
            "--auto-start",
            "--tdc-frequency",
            str(tdc_frequency),
            "--cps",
            str(cps),
            "--duration",
            str(duration),
            "--no-count",  # Better performance for UI
        ]

        return self._start_process(
            name="simulator",
            program=sys.executable,
            args=args,
            working_dir=self._project_root,
        )

    def start_live_cli(self, replay_file: Optional[str] = None) -> bool:
        """Start live-cli for real detector or replay mode."""
        live_cli = self._project_root / "ASI" / "live-cli"

        if not live_cli.exists():
            logger.error(f"live-cli not found: {live_cli}")
            return False

        # parameters recommended by Henrique 2025-12-15.  Default would be []
        # (i.e. live-cli's own defaults, --bin-width-exp 2 --max-delay-bins 3).
        # NOTE 2026-05-01: enabling these to test whether they affect the
        # ~45 s upstream-bolus pattern we see in /tmp/flush_pacing_*.json.
        # Math suggests they shouldn't (default and these both give ~5 ms
        # of total sort-buffer latency — same depth, just different bin
        # granularity), but enabling them removes one variable.  Revert by
        # setting `args = []` below.
        args = ["--bin-width-exp", "0", "--max-delay-bins", "12"]

        if replay_file:
            args = ["--source-files", replay_file]

        return self._start_process(
            name="live-cli",
            program=str(live_cli),
            args=args,
            working_dir=live_cli.parent,
        )

    def start_acquisition(self, duration: int, output_dir: str, preview: bool = False) -> bool:

        args = [
            "-m",
            "splash_timepix.serval_client.acq",
            "-time",
            str(duration),
        ]
        if preview:
            args.append("--preview")
        else:
            args.extend(["-output", output_dir])

        return self._start_process(
            name="acquisition",
            program=sys.executable,
            args=args,
            working_dir=self._project_root,
        )

    def stop_process(self, name: str) -> None:
        if name in self.processes:
            proc = self.processes[name]
            if proc.state() != QProcess.ProcessState.NotRunning:
                logger.info(f"Stopping process: {name}")
                proc.terminate()
                if not proc.waitForFinished(5000):
                    logger.warning(f"Process {name} didn't terminate, killing")
                    proc.kill()

    def stop_all(self) -> None:
        for name in list(self.processes.keys()):
            self.stop_process(name)

    def is_running(self, name: str) -> bool:
        if name in self.processes:
            return self.processes[name].state() != QProcess.ProcessState.NotRunning
        return False

    def _start_process(self, name: str, program: str, args: list, working_dir: Path) -> bool:
        if name in self.processes:
            if self.processes[name].state() != QProcess.ProcessState.NotRunning:
                logger.warning(f"Process {name} already running")
                return False

        proc = QProcess(self)
        proc.setWorkingDirectory(str(working_dir))
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

        proc.readyReadStandardOutput.connect(lambda: self._on_output(name, proc))
        proc.finished.connect(lambda code, status: self._on_finished(name, code))
        proc.started.connect(lambda: self._on_started(name))

        self.processes[name] = proc

        logger.info(f"Starting process {name}: {program} {' '.join(args)}")
        proc.start(program, args)

        if not proc.waitForStarted(5000):
            logger.error(f"Failed to start {name}: {proc.errorString()}")
            del self.processes[name]
            proc.deleteLater()
            return False
        return True

    def _on_started(self, name: str) -> None:
        logger.info(f"Process started: {name}")
        self.process_started.emit(name)

    def _on_finished(self, name: str, exit_code: int) -> None:
        logger.info(f"Process finished: {name} (exit code: {exit_code})")
        self.process_stopped.emit(name, exit_code)

    def _on_output(self, name: str, proc: QProcess) -> None:
        data = proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
        if data:
            self.process_output.emit(name, data)


# =============================================================================
# Centroider sweep (tools/centroider) integration
# =============================================================================

# Project root: .../splash_timepix_dev (workers.py is at src/splash_timepix/ui/).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CENTROIDER_DIR = _PROJECT_ROOT / "tools" / "centroider"

_centroider_api: Optional[ModuleType] = None


def import_centroider_api() -> ModuleType:
    """Import the centroider backend (tools/centroider/api.py) in-process.

    The centroider package lives outside the installed ``splash_timepix``
    package, so its directory is added to ``sys.path`` (api.py also relies on
    that for its sibling imports) and the module is loaded from its file path.
    The module is cached after the first successful import.
    """
    global _centroider_api
    if _centroider_api is not None:
        return _centroider_api

    api_path = _CENTROIDER_DIR / "api.py"
    if not api_path.exists():
        raise FileNotFoundError(f"centroider backend not found at: {api_path}")

    if str(_CENTROIDER_DIR) not in sys.path:
        sys.path.insert(0, str(_CENTROIDER_DIR))

    spec = importlib.util.spec_from_file_location("centroider_api", api_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load centroider backend from {api_path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass annotation resolution (which looks the
    # module up in sys.modules via cls.__module__) succeeds.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _centroider_api = module
    return module


class CentroiderWorker(QThread):
    """Runs a centroider sweep in a background thread, calling the centroider
    backend (``tools/centroider/api.run_sweep``) and relaying its progress
    callbacks as Qt signals.

    All sweep orchestration lives in the centroider backend; this worker is
    only the threading + signal glue.
    """

    # ProgressEvent emitted when a run begins (status is None).
    progress = Signal(object)
    # ProgressEvent emitted when a run finishes (status is "ok"/"failed"/"skipped").
    combo_finished = Signal(object)
    # SweepResult emitted once the whole sweep completes.
    sweep_finished = Signal(object)
    error_occurred = Signal(str)

    def __init__(
        self,
        input_file: str,
        eps_s: str,
        eps_t: str,
        tpx3dump: Optional[str] = None,
        output_parent: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._input_file = input_file
        self._eps_s = eps_s
        self._eps_t = eps_t
        self._tpx3dump = tpx3dump
        self._output_parent = output_parent

    def run(self) -> None:
        try:
            api = import_centroider_api()
        except Exception as exc:  # noqa: BLE001
            self.error_occurred.emit(f"Failed to load centroider backend: {exc}")
            return

        # Default the output parent to the input file's directory.
        output_parent = self._output_parent or str(Path(self._input_file).parent)

        def _callback(event) -> None:
            if event.phase == "done":
                # Final sweep-complete marker; sweep_finished covers this.
                return
            if event.status is None:
                self.progress.emit(event)
            else:
                self.combo_finished.emit(event)

        try:
            result = api.run_sweep(
                input_file=self._input_file,
                output_parent=output_parent,
                eps_t_list=self._eps_t,
                eps_s_list=self._eps_s,
                tpx3dump=self._tpx3dump,
                progress_callback=_callback,
            )
        except Exception as exc:  # noqa: BLE001
            self.error_occurred.emit(str(exc))
            return

        self.sweep_finished.emit(result)
