#!/usr/bin/env python3
"""
Demo script to run both server and source for testing.

This script demonstrates the socket server functionality.
"""

import os
import subprocess
import time
from pathlib import Path

# Get the directory of the current script, python venv, and project
SCRIPT_DIR = Path(__file__).parent.resolve()
if os.name == "nt":  # windows
    VENV_PYTHON = SCRIPT_DIR / ".venv" / "Scripts" / "python.exe"
else:
    VENV_PYTHON = SCRIPT_DIR / ".venv" / "bin" / "python"
PROJECT_DIR = SCRIPT_DIR / "src"


def run_source():
    """Run the test source in a subprocess."""
    source_cmd = [str(VENV_PYTHON), "-m", "splash_timepix.test_source"]
    print("📡 Starting test source...")
    source_process = subprocess.Popen(
        source_cmd,
        cwd=str(PROJECT_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return source_process


def run_server():
    """Run the server in a subprocess."""
    server_cmd = [str(VENV_PYTHON), "-m", "splash_timepix.example"]
    print("🚀 Starting server...")
    server_process = subprocess.Popen(
        server_cmd,
        cwd=str(PROJECT_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return server_process


def demo_run():
    """Run both server and source for interactive demo."""
    server_process = run_server()
    time.sleep(2)
    source_process = run_source()
    print("\n💡 Interact with the source window to set parameters and start sending data.")
    print("🛑 Press Ctrl+C to stop both server and source.")
    try:
        source_process.wait()
    except KeyboardInterrupt:
        print("\n🛑 Stopping demo...")
    finally:
        if server_process.poll() is None:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()


def main():
    """Main function to choose demo mode."""
    print("Socket Data Server Demo")
    print("=" * 30)
    print("Choose demo mode:")
    print("1. Demo (configure source and trigger sending of test data)")
    print("2. Server only (start server, run source manually)")
    print("3. Source only (connect to existing server)")

    try:
        choice = input("\nEnter choice (1-3): ").strip()

        if choice == "1":
            demo_run()
        elif choice == "2":
            print("🚀 Starting server only...")
            print("💡 In another terminal, run: python -m splash_timepix.test_source")
            server_process = run_server()
            try:
                server_process.wait()
            except KeyboardInterrupt:
                print("\n🛑 Stopping server...")
                server_process.terminate()
        elif choice == "3":
            print("📡 Starting source only...")
            print("💡 Make sure server is running in another terminal")
            source_process = run_source()
            try:
                source_process.wait()
            except KeyboardInterrupt:
                print("\n🛑 Stopping source...")
                source_process.terminate()
        else:
            print("❌ Invalid choice")

    except KeyboardInterrupt:
        print("\n🛑 Exiting...")
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()
