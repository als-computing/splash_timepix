#!/usr/bin/env python3
"""Demo script to run both server and client for testing.

This script demonstrates the socket server functionality.
"""

import subprocess
import time
import os
from pathlib import Path

# Get the directory of the current script, python venv, and project
SCRIPT_DIR = Path(__file__).parent.resolve()
if os.name == "nt": # windows
    VENV_PYTHON = SCRIPT_DIR / ".venv" / "Scripts" / "python.exe"
else:
    VENV_PYTHON = SCRIPT_DIR / ".venv" / "bin" / "python"
PROJECT_DIR = SCRIPT_DIR / "src"

def run_server():
    """Run the server in a subprocess."""
    server_cmd = [str(VENV_PYTHON), "-m", "splash_timepix.example"]

    print("🚀 Starting server...")
    server_process = subprocess.Popen(
        server_cmd, cwd=str(PROJECT_DIR)
    )
    return server_process


def run_client():
    """Run the test client in a subprocess."""
    client_cmd = [str(VENV_PYTHON), "-m", "splash_timepix.test_client"]

    print("📡 Starting test client...")
    client_process = subprocess.Popen(
        client_cmd,
        cwd=str(PROJECT_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return client_process


def demo_automatic():
    """Run an automatic demo."""
    print("🎬 Starting automatic demo...")
    print("=" * 50)

    # Start server
    server_process = run_server()

    try:
        # Give server time to start
        time.sleep(2)

        # Start client and send some test data
        client_process = run_client()

        # Send commands to client to auto-send data
        commands = [
            "1\n",  # Choose test data mode
        ]

        for cmd in commands:
            if client_process.poll() is None:  # Process still running
                client_process.stdin.write(cmd)
                client_process.stdin.flush()
                time.sleep(1)

        # Wait a bit for client to finish
        try:
            client_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            client_process.terminate()

        print("\n📊 Demo completed! Check server output above.")
        print("🛑 Press Ctrl+C to stop the server...")

        # Keep server running until user stops it
        server_process.wait()

    except KeyboardInterrupt:
        print("\n🛑 Stopping demo...")

    finally:
        # Clean up processes
        if server_process.poll() is None:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()


def demo_interactive():
    """Run an interactive demo where user controls the client."""
    print("🎮 Starting interactive demo...")
    print("=" * 50)

    # Start server
    server_process = run_server()

    try:
        # Give server time to start
        time.sleep(2)

        print("\n📡 Server is running. Now starting interactive client...")
        print("💡 You can send messages to test the server.")
        print("🛑 Press Ctrl+C to stop both server and client.")

        # Start interactive client
        client_process = run_client()

        # Wait for client to finish or be interrupted
        client_process.wait()

    except KeyboardInterrupt:
        print("\n🛑 Stopping demo...")

    finally:
        # Clean up processes
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
    print("1. Automatic demo (sends test data automatically)")
    print("2. Interactive demo (you control the client)")
    print("3. Server only (start server, run client manually)")
    print("4. Client only (connect to existing server)")

    try:
        choice = input("\nEnter choice (1-4): ").strip()

        if choice == "1":
            demo_automatic()
        elif choice == "2":
            demo_interactive()
        elif choice == "3":
            print("🚀 Starting server only...")
            print("💡 In another terminal, run: python -m splash_timepix.test_client")
            server_process = run_server()
            try:
                server_process.wait()
            except KeyboardInterrupt:
                print("\n🛑 Stopping server...")
                server_process.terminate()
        elif choice == "4":
            print("📡 Starting client only...")
            print("💡 Make sure server is running in another terminal")
            client_process = run_client()
            try:
                client_process.wait()
            except KeyboardInterrupt:
                print("\n🛑 Stopping client...")
                client_process.terminate()
        else:
            print("❌ Invalid choice")

    except KeyboardInterrupt:
        print("\n🛑 Exiting...")
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()
