"""
Stop a running TimePix3 acquisition gracefully.

Usage:
    tpx-stop
"""

import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(help="Stop a running TimePix3 acquisition")


def get_project_root() -> Path:
    """Get the project root directory (parent of src/)."""
    # This file is in src/splash_timepix/
    return Path(__file__).parent.parent.parent


@app.command()
def main():
    """
    Stop the current TimePix3 acquisition gracefully.

    Calls the Serval /measurement/stop endpoint to halt data taking.
    """
    project_root = get_project_root()
    stop_script = project_root / "ASI" / "serval_client" / "stop.py"

    if not stop_script.exists():
        print(f"ERROR: Stop script not found at: {stop_script}")
        raise typer.Exit(1)

    print("Stopping acquisition...")

    try:
        result = subprocess.run(
            [sys.executable, str(stop_script)],
            cwd=stop_script.parent,
            capture_output=True,
            text=True,
        )

        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())

        if result.returncode == 0:
            print("Acquisition stopped successfully")
        else:
            print(f"ERROR: Stop script exited with code {result.returncode}")
            raise typer.Exit(1)

    except Exception as e:
        print(f"ERROR: Failed to stop acquisition: {e}")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
