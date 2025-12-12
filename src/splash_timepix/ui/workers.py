"""QThread workers for background tasks.

Workers communicate with the UI via Qt signals to ensure thread safety.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import zmq
import msgpack
import requests
from PySide6.QtCore import QThread, Signal, QProcess, QObject

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
                    metadata_bytes = socket.recv()
                    array_bytes = socket.recv()
                    
                    if not self._connected:
                        self._connected = True
                        self.connection_changed.emit(True)
                    
                    metadata = msgpack.unpackb(metadata_bytes)
                    shape = tuple(metadata['shape'])
                    dtype = metadata['dtype']
                    array = np.frombuffer(array_bytes, dtype=dtype).reshape(shape)
                    
                    flush_data = FlushData(array=array.copy(), metadata=metadata)
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
                        state=msg.get('state', 'unknown'),
                        uptime_s=msg.get('uptime_s', 0.0),
                        data_port=msg.get('data_port', 0),
                        tcp_port=msg.get('tcp_port', 0),
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


# =============================================================================
# Serval Poller Worker  
# =============================================================================

class ServalPollerWorker(QThread):
    """Background thread that polls Serval server for status."""
    
    status_updated = Signal(object)  # ServalStatus
    connection_changed = Signal(bool)
    
    def __init__(self, base_url: str = "http://localhost:8080", 
                 poll_interval: float = 1.0, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.poll_interval = poll_interval
        self._running = False
        self._connected = False
    
    def run(self):
        import sys
        import requests
        
        self._running = True
        
        # Give Serval time to start up
        time.sleep(2.0)
        
        while self._running:
            try:
                # Direct HTTP request instead of using lib.py
                response = requests.get(
                    f"{self.base_url}/dashboard",
                    timeout=5.0
                )
                response.raise_for_status()
                dashboard = response.json()
                
                # Measurement can be null when no acquisition is running
                measurement = dashboard.get("Measurement") or {}
                
                status = ServalStatus(
                    connected=True,
                    pixel_event_rate=measurement.get("PixelEventRate", 0) or 0,
                    tdc1_event_rate=measurement.get("Tdc1EventRate", 0) or 0,
                    tdc2_event_rate=measurement.get("Tdc2EventRate", 0) or 0,
                    frame_count=measurement.get("FrameCount", 0) or 0,
                    elapsed_time=measurement.get("ElapsedTime", 0.0) or 0.0,
                    time_left=measurement.get("TimeLeft", 0.0) or 0.0,
                    status=measurement.get("Status") or "IDLE",
                )
                
                if not self._connected:
                    self._connected = True
                    self.connection_changed.emit(True)
                
                self.status_updated.emit(status)
                
            except requests.exceptions.RequestException as e:
                logger.debug(f"Serval poll error: {e}")
                if self._connected:
                    self._connected = False
                    self.connection_changed.emit(False)
                status = ServalStatus(connected=False, error=str(e))
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
            working_dir=serval_dir
        )
    
    def start_streaming_server(self, tdc_frequency: float, tdc_channel: int = 1,
                               tdc_edge: str = "rising", collapse_y: bool = True,
                               exit_on_disconnect: bool = True) -> bool:
        args = [
            "-m", "splash_timepix.app",
            "--tdc-frequency", str(tdc_frequency),
            "--tdc-ch", str(tdc_channel),
            "--tdc-edge", tdc_edge,
        ]
        if collapse_y:
            args.append("--collapse-y")
        if exit_on_disconnect:
            args.append("--exit-on-disconnect")
        
        return self._start_process(
            name="streaming",
            program="python",
            args=args,
            working_dir=self._project_root
        )
    
    def start_simulator(self, tdc_frequency: float = 1.0, cps: float = 1000.0,
                        duration: int = 60) -> bool:
        """Start the simulator in auto-start mode."""
        args = [
            "-m", "splash_timepix.simulator_cli",
            "--auto-start",
            "--tdc-frequency", str(tdc_frequency),
            "--cps", str(cps),
            "--duration", str(duration),
            "--no-count",  # Better performance for UI
        ]
        
        return self._start_process(
            name="simulator",
            program="python",
            args=args,
            working_dir=self._project_root
        )
    
    def start_live_cli(self, replay_file: Optional[str] = None) -> bool:
        """Start live-cli for real detector or replay mode."""
        live_cli = self._project_root / "ASI" / "live-cli"
        
        if not live_cli.exists():
            logger.error(f"live-cli not found: {live_cli}")
            return False
        
        args = []
        if replay_file:
            args = ["--source-files", replay_file]
        
        return self._start_process(
            name="live-cli",
            program=str(live_cli),
            args=args,
            working_dir=live_cli.parent
        )
    
    def start_acquisition(self, duration: int, output_dir: str, 
                          preview: bool = False) -> bool:
        acq_script = self._project_root / "ASI" / "serval_client" / "acq.py"
        
        if not acq_script.exists():
            logger.error(f"acq.py not found: {acq_script}")
            return False
        
        args = [str(acq_script), "-time", str(duration)]
        if preview:
            args.append("--preview")
        else:
            args.extend(["-output", output_dir])
        
        return self._start_process(
            name="acquisition",
            program="python",
            args=args,
            working_dir=acq_script.parent
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
    
    def _start_process(self, name: str, program: str, args: list, 
                       working_dir: Path) -> bool:
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
        
        return proc.waitForStarted(5000)
    
    def _on_started(self, name: str) -> None:
        logger.info(f"Process started: {name}")
        self.process_started.emit(name)
    
    def _on_finished(self, name: str, exit_code: int) -> None:
        logger.info(f"Process finished: {name} (exit code: {exit_code})")
        self.process_stopped.emit(name, exit_code)
    
    def _on_output(self, name: str, proc: QProcess) -> None:
        data = proc.readAllStandardOutput().data().decode('utf-8', errors='replace')
        if data:
            self.process_output.emit(name, data)
            