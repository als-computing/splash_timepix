#!/bin/bash
# TimePix3 UI Launcher
# 
# Launches the TimePix3 Acquisition UI with Serval autostart enabled.
# If Serval is already running, it will be detected and not started again.
#
# Usage:
#   ./scripts/tpxui_launcher.sh
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

# Activate venv and launch UI
cd "$PROJECT_DIR"
source "$VENV_PATH/bin/activate"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  TimePix3 Acquisition UI"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Starting UI with Serval autostart..."
echo ""

# Launch the UI with autostart-serval flag
python -m splash_timepix.ui.main --autostart-serval
