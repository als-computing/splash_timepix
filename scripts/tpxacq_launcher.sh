#!/bin/bash
# TimePix3 Acquisition Launcher
#
# Opens a terminal with the splash_timepix venv activated.
# User can start and stop acquisitions from this terminal.
#
# Usage:
#   ./scripts/tpx-acq_launcher.sh
#   Or create a desktop shortcut pointing to this script

# Get the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Project root is one level up from scripts/
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Path to virtual environment
VENV_PATH="$PROJECT_DIR/.venv"

# Check if venv exists
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Please create it with: python3 -m venv $VENV_PATH"
    exit 1
fi

# Path to Serval
SERVAL_DIR="$PROJECT_DIR/ASI"
SERVAL_JAR="serval-4.1.1.jar"

# Check if Serval jar exists
if [ ! -f "$SERVAL_DIR/$SERVAL_JAR" ]; then
    echo "Warning: Serval not found at $SERVAL_DIR/$SERVAL_JAR"
    echo "Serval terminal will not be started."
    SERVAL_FOUND=false
else
    SERVAL_FOUND=true
fi

# Check if Serval is running by looking for the java process
if pgrep -f "serval-4.1.1.jar" > /dev/null; then
    echo "Found Serval process already running - skipping startup"
    SERVAL_RUNNING=true
else
    SERVAL_RUNNING=false
fi

# Start Serval in a separate terminal (if not already running)
if [ "$SERVAL_FOUND" = true ] && [ "$SERVAL_RUNNING" = false ]; then
    gnome-terminal --title="Serval Server" -- bash -c "
        cd '$SERVAL_DIR'
        echo '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
        echo '  Serval Server'
        echo '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
        echo ''
        java -Xmx8G -jar $SERVAL_JAR
        exec bash
    "
elif [ "$SERVAL_RUNNING" = true ]; then
    echo "Skipping Serval startup - already running"
fi

# Open main terminal, cd to project dir, activate venv, and give user a shell
gnome-terminal --title="TimePix3 Acquisition" -- bash -c "
    cd '$PROJECT_DIR'
    source '$VENV_PATH/bin/activate'
    echo '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
    echo '  TimePix3 Acquisition Environment'
    echo '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
    echo ''
    echo '  Run an acquisition:'
    echo '    tpx-acq -tdc <frequency> [-t <seconds>] [-o <output_dir>]'
    echo ''
    echo '  Example:'
    echo '    tpx-acq -tdc 10'
    echo '    tpx-acq -tdc 10 -t 60'
    echo '    tpx-acq -tdc 10 -t 60 -o /path/to/data'
    echo ''
    echo '  Help:'
    echo '    tpx-acq --help'
    echo ''
    echo '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
    echo ''
    exec bash
"
