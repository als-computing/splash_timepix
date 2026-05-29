#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_example.sh — Zero-argument convenience runner for the tpx3 sweep optimizer
#
# Runs a 9-combination sweep (3 time gaps × 3 pixel distances) over the
# small 21 MB sample file. Results land in SampleData/h5s/.
#
# Usage:
#   bash tools/centroider/run_example.sh           # full 9-combo sweep
#   bash tools/centroider/run_example.sh --dry-run # preview commands only
#   bash tools/centroider/run_example.sh --skip-existing --keep-going
#
# Override paths via env vars:
#   TPX3_INPUT    — path to .tpx3 file      (default: SampleData/tpx3/sample21MB.tpx3)
#   H5_OUTPUT_DIR — output directory        (default: SampleData/h5s)
#   TPX3DUMP      — path to tpx3dump binary (default: luna 0.4.3 bin)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Paths — override any of these via env vars before calling this script
TPX3_INPUT="${TPX3_INPUT:-/home/tpx/Desktop/tpx3LOCAL/SampleData/tpx3/sample21MB.tpx3}"
H5_OUTPUT_DIR="${H5_OUTPUT_DIR:-/home/tpx/Desktop/tpx3LOCAL/SampleData/h5s}"
export TPX3DUMP="${TPX3DUMP:-/home/tpx/Desktop/tpx3LOCAL/software/luna/0.4.3/bin/tpx3dump}"

# Sweep parameters — keep eps-t in the ns–µs range.
# Large values (ms and above) make DFSCluster exponentially slow on Tpx3 data.
EPS_T="20ns,100ns,500ns"
EPS_S="1,2,3"

echo "========================================"
echo "  tpx3 Sweep — Example Run"
echo "========================================"
echo "  Input       : $TPX3_INPUT"
echo "  Output dir  : $H5_OUTPUT_DIR"
echo "  tpx3dump    : $TPX3DUMP"
echo "  eps-t       : $EPS_T"
echo "  eps-s       : $EPS_S"
echo "========================================"
echo ""

# Forward any extra args (e.g. --dry-run, --skip-existing, --keep-going)
python3 "$SCRIPT_DIR/sweep.py" \
    -i "$TPX3_INPUT" \
    -o "$H5_OUTPUT_DIR" \
    -t "$EPS_T" \
    -s "$EPS_S" \
    "$@"
