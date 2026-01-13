"""
Acquisition orchestrator for TimePix3 data streaming.

Spawns streaming server, live-cli and acquisition script 
in separate terminal windows.

Usage:
    tpx-acq -tdc <frequency> -t <seconds>
"""

import subprocess
import time
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="TimePix3 acquisition orchestrator")

# PID file location
PID_FILE = Path("/tmp/tpx_acq_pids")

# Default ports (should match app.py defaults)
DEFAULT_HEARTBEAT_PORT = 5658


def get_project_root() -> Path:
    """Get the project root directory (parent of src/)."""
    # This file is in src/splash_timepix/
    return Path(__file__).parent.parent.parent


def kill_old_windows() -> None:
    """Kill any terminal windows from previous acquisitions."""
    if not PID_FILE.exists():
        return

    try:
        pids = PID_FILE.read_text().strip().split("\n")
        for pid in pids:
            if pid:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass  # Process already dead or invalid PID
        PID_FILE.unlink()
    except Exception:
        pass  # Ignore errors reading/deleting PID file


def save_pids(pids: list[int]) -> None:
    """Save PIDs to file for later cleanup."""
    PID_FILE.write_text("\n".join(str(p) for p in pids))


def spawn_terminal(title: str, command: str, working_dir: Path) -> Optional[int]:
    """
    Spawn a new gnome-terminal window with the given command.
    
    Args:
        title: Window title
        command: Command to run in the terminal
        working_dir: Working directory for the command
    
    Returns:
        PID of the terminal process, or None if failed
    """
    # Use bash -c to run command, exec bash keeps window open after
    full_command = f"cd '{working_dir}' && {command}; exec bash"
    
    try:
        proc = subprocess.Popen([
            "gnome-terminal",
            f"--title={title}",
            "--",
            "bash", "-c", full_command
        ])
        return proc.pid
    except Exception as e:
        print(f"ERROR: Failed to spawn terminal '{title}': {e}")
        return None


def wait_for_server_ready(port: int = DEFAULT_HEARTBEAT_PORT, 
                          timeout: float = 30.0) -> bool:
    """Wait for the streaming server to become ready via heartbeat.
    
    Args:
        port: Heartbeat ZMQ port
        timeout: Maximum seconds to wait
        
    Returns:
        True if server became ready, False if timeout
    """
    try:
        from splash_timepix.heartbeat import wait_for_ready
        return wait_for_ready(port=port, timeout=timeout)
    except ImportError:
        # Fallback to simple sleep if heartbeat module not available
        print(f"WARNING: Heartbeat module not available, using fixed delay")
        time.sleep(5.0)
        return True


@app.command()
def main(
    tdc: float = typer.Option(
        ...,
        "-tdc", "--tdc-frequency",
        help="TDC trigger frequency in Hz"
    ),
    t: int = typer.Option(
        19008000, # longest possible acquisition 220 days
        "-t", "--time",
        help="Acquisition time in seconds"
    ),
    output: str = typer.Option(
        "/home/tpx/Desktop/tpx3LOCAL/data",
        "-o", "--output",
        help="Output directory for data files"
    ),
    preview: bool = typer.Option(
        False,
        "--preview",
        help="Preview mode: stream only, no file writing"
    ),
    server_timeout: float = typer.Option(
        30.0,
        "--server-timeout",
        help="Seconds to wait for streaming server to become ready"
    ),
    livecli_delay: float = typer.Option(
        1.0,
        "--livecli-delay",
        help="Seconds to wait after server ready before launching live-cli"
    ),
):
    """
    Run a TimePix3 acquisition.

    Spawns (in this order) streaming server, live-cli and acquisition script
    in separate terminal windows. Waits for server heartbeat before proceeding.

    Example:
        tpx-acq -tdc 100000
        tpx-acq -tdc 100000 -t 60
        tpx-acq -tdc 100000 -t 60 -o /path/to/tpx3/file
        tpx-acq -tdc 100000 --preview  # Stream only, no file saving
    """
    project_root = get_project_root()
    
    # Define paths (relative to project root)
    live_cli_path = "./ASI/live-cli" # Path to live-cli executable
    acq_script_path = "./ASI/serval_client/acq.py" # Path to acquisition script

    # Resolve paths
    live_cli = project_root / live_cli_path
    acq_script = project_root / acq_script_path
    
    # Validate paths
    if not live_cli.exists():
        print(f"ERROR: live-cli not found at: {live_cli}")
        raise typer.Exit(1)
    
    if not acq_script.exists():
        print(f"ERROR: Acquisition script not found at: {acq_script}")
        raise typer.Exit(1)
    
    # Kill any old terminal windows from previous runs
    print("Cleaning up old terminal windows...")
    kill_old_windows()
    
    pids = []
    
    mode_str = "PREVIEW (no file saving)" if preview else "ACQUISITION"
    print(f"Starting {mode_str}: {t}s at {tdc} Hz TDC")
    if not preview:
        print(f"Output directory: {output}")
    print()
    
    # 1. Spawn streaming server window
    print("Starting streaming server...")
    server_command = (
        f"python -m splash_timepix.app "
        f"--tdc-frequency {tdc} "
        f"--exit-on-disconnect"
    )
    pid = spawn_terminal("TimePix3 Server", server_command, project_root)
    if pid:
        pids.append(pid)
    
    # 2. Wait for server to be ready via heartbeat
    print(f"Waiting for server to be ready (timeout: {server_timeout}s)...")
    if not wait_for_server_ready(timeout=server_timeout):
        print("ERROR: Server did not become ready in time")
        # Kill spawned processes
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise typer.Exit(1)
    
    print("Server ready!")
    
    # Small additional delay for stability
    if livecli_delay > 0:
        print(f"Waiting {livecli_delay}s before starting live-cli...")
        time.sleep(livecli_delay)
    
    # 3. Spawn live-cli window
    print("Starting live-cli...")
    livecli_command = f"'{live_cli}'"
    pid = spawn_terminal("TimePix3 Live-CLI", livecli_command, live_cli.parent)
    if pid:
        pids.append(pid)
    
    # Small delay for live-cli to connect
    time.sleep(1.0)
    
    # 4. Spawn acquisition window
    print("Starting acquisition...")
    if preview:
        # Preview mode: use streaming-only destination
        acq_command = f"python '{acq_script}' -time {t} --preview"
    else:
        # Full acquisition mode
        acq_command = f"python '{acq_script}' -time {t} -output '{output}'"
    
    pid = spawn_terminal("TimePix3 Acquisition", acq_command, acq_script.parent)
    if pid:
        pids.append(pid)
    
    # Save PIDs for cleanup on next run
    if pids:
        save_pids(pids)
    
    print()
    print("All processes started!")
    print()
    print("To stop acquisition early:")
    print("   tpx-stop")
    print()
    print("To run another acquisition:")
    print("   tpx-acq -tdc <frequency> [-t <seconds>] [-o <path/to/data>]")
    print()
    print("To preview without saving:")
    print("   tpx-acq -tdc <frequency> --preview")


if __name__ == "__main__":
    app()
    