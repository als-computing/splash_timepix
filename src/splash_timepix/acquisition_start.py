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
        print(f"❌ Failed to spawn terminal '{title}': {e}")
        return None


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
    server_delay: float = typer.Option(
        5.0,
        "--server-delay",
        help="Seconds to wait after starting server before launching live-cli"
    ),
    livecli_delay: float = typer.Option(
        1.0,
        "--livecli-delay",
        help="Seconds to wait after starting live-cli before launching acquisition"
    ),
):
    """
    Run a TimePix3 acquisition.

    Spawns (in this order) streaming server, live-cli and acquisition script
    in separate terminal windows.

    Example:
        tpx-acq -tdc 100000
        tpx-acq -tdc 100000 -t 60
        tpx-acq -tdc 100000 -t 60 -o /path/to/tpx3/file
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
        print(f"❌ live-cli not found at: {live_cli}")
        raise typer.Exit(1)
    
    if not acq_script.exists():
        print(f"❌ Acquisition script not found at: {acq_script}")
        raise typer.Exit(1)
    
    # Kill any old terminal windows from previous runs
    print("🧹 Cleaning up old terminal windows...")
    kill_old_windows()
    
    pids = []
    
    print(f"🚀 Starting acquisition: {t}s at {tdc} Hz TDC")
    print(f"📁 Output directory: {output}")
    print()
    
    # Spawn streaming server window
    print("⏳ Spawning streaming server window...")
    #server_command = f"python -m splash_timepix.app --tdc-frequency {tdc}" # ZMQ
    server_command = f"python -m splash_timepix.app --plot --tdc-frequency {tdc}" # plot
    pid = spawn_terminal("TimePix3 Server", server_command, project_root)
    if pid:
        pids.append(pid)
    
    # Spawn live-cli window - it will wait, then run
    print(f"⏳ Spawning live-cli window (will start in {server_delay}s)...")
    livecli_command = f"sleep {server_delay} && '{live_cli}'"
    pid = spawn_terminal("TimePix3 Live-CLI", livecli_command, live_cli.parent)
    if pid:
        pids.append(pid)
    
    # Spawn acquisition window - it will wait, then run
    total_delay = server_delay + livecli_delay
    print(f"⏳ Spawning acquisition window (will start in {total_delay}s)...")
    acq_command = f"sleep {total_delay} && python '{acq_script}' -time {t} -output '{output}'"
    pid = spawn_terminal("TimePix3 Acquisition", acq_command, acq_script.parent)
    if pid:
        pids.append(pid)
    
    # Save PIDs for cleanup on next run
    if pids:
        save_pids(pids)
    
    print("✅ All windows spawned!")
    print()
    print("🛑 To stop acquisition early:")
    print(f"tpx-stop")
    print()
    print("🔄 To run another acquisition:")
    print(f"tpx-acq -tdc <frequency> [-t <seconds>] [-o <path/to/data>]")


if __name__ == "__main__":
    app()
