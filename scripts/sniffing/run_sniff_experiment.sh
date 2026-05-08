#!/usr/bin/env bash
# One-shot orchestrator for the sniff-the-pipeline experiment.
#
# Topology:
#   Serval (TCP client)   ──►  127.0.0.1:7070   ◄──  live-cli   ──►  127.0.0.1:9090   ◄──  splash_timepix.app
#                              (hop A)                                (hop B)
#
# This script:
#   1. Starts tcpdump on lo capturing both ports.
#   2. Starts splash_timepix.app on :9090 / ZMQ :5657 / heartbeat :5658.
#   3. Starts live-cli with Henrique's recommended args.
#   4. Starts flush_pacing_listener.py (so it captures the start_msg).
#   5. Drives Serval via acq.py --preview -time DURATION (blocks until done).
#   6. Tears everything down and runs scripts/sniffing/sniff_analyze.py on the pcap+JSON.
#
# Pre-reqs:
#   * Serval must be running on :8080 (the UI's --autostart-serval handles this)
#   * tcpdump must be runnable as plain user.  Either:
#       sudo setcap cap_net_raw,cap_net_admin=eip /usr/bin/tcpdump
#     or NOPASSWD sudo for tcpdump (script auto-detects and prepends sudo).
#   * Ports 7070 / 9090 must be free (UI must NOT have an active acquisition)
#
# Usage:
#   bash scripts/sniffing/run_sniff_experiment.sh                   # 90 s default
#   DURATION_S=120 bash scripts/sniffing/run_sniff_experiment.sh
#   TDC_FREQ=100 DURATION_S=60 bash scripts/sniffing/run_sniff_experiment.sh

set -uo pipefail

DURATION_S="${DURATION_S:-90}"
TDC_FREQ="${TDC_FREQ:-1000}"
FLUSH_INTERVAL="${FLUSH_INTERVAL:-1.0}"
LIVECLI_BIN_WIDTH_EXP="${LIVECLI_BIN_WIDTH_EXP:-0}"
LIVECLI_MAX_DELAY_BINS="${LIVECLI_MAX_DELAY_BINS:-12}"
LIVECLI_EXTRA_ARGS="${LIVECLI_EXTRA_ARGS:-}"
TAG="${TAG:-}"
TS=$(date +%s)
SUFFIX="${TS}${TAG:+_$TAG}"
PCAP=/tmp/sniff_${SUFFIX}.pcap
LISTENER_JSON=/tmp/flush_pacing_${SUFFIX}.json
LOG_DIR=/tmp/sniff_logs_${SUFFIX}
mkdir -p "$LOG_DIR"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

PY="$PROJECT_ROOT/.venv/bin/python"
LIVECLI="$PROJECT_ROOT/ASI/live-cli"

if [ ! -x "$PY" ]; then
    echo "ERROR: venv python not found at $PY" >&2
    exit 1
fi
if [ ! -x "$LIVECLI" ]; then
    echo "ERROR: live-cli not found at $LIVECLI" >&2
    exit 1
fi

TCPDUMP=tcpdump
TCPDUMP_PREFIX=()
if getcap /usr/bin/tcpdump 2>/dev/null | grep -q 'cap_net_raw'; then
    echo "[init] tcpdump has cap_net_raw — no sudo needed"
elif sudo -n true 2>/dev/null; then
    TCPDUMP_PREFIX=(sudo -n)
    echo "[init] tcpdump will use NOPASSWD sudo"
else
    echo "ERROR: tcpdump cannot capture as plain user, and sudo would prompt." >&2
    echo "Run one of these and retry:" >&2
    echo "  sudo setcap cap_net_raw,cap_net_admin=eip /usr/bin/tcpdump" >&2
    echo "  echo 'tpx ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/99-tpx-cursor-debug" >&2
    exit 1
fi

cleanup() {
    local rc=$?
    echo ""
    echo "[cleanup] stopping processes..."
    [ -n "${LISTENER_PID:-}" ] && kill -INT "$LISTENER_PID" 2>/dev/null || true
    [ -n "${TCPDUMP_PID:-}" ] && "${TCPDUMP_PREFIX[@]}" kill -INT "$TCPDUMP_PID" 2>/dev/null || true
    [ -n "${LIVECLI_PID:-}" ] && kill -TERM "$LIVECLI_PID" 2>/dev/null || true
    [ -n "${APP_PID:-}" ] && kill -TERM "$APP_PID" 2>/dev/null || true
    sleep 1
    [ -n "${LISTENER_PID:-}" ] && wait "$LISTENER_PID" 2>/dev/null || true
    [ -n "${TCPDUMP_PID:-}" ] && wait "$TCPDUMP_PID" 2>/dev/null || true
    [ -n "${LIVECLI_PID:-}" ] && wait "$LIVECLI_PID" 2>/dev/null || true
    [ -n "${APP_PID:-}" ] && wait "$APP_PID" 2>/dev/null || true
    return $rc
}
trap cleanup EXIT INT TERM

echo "=================================================================="
echo "SNIFF EXPERIMENT  (duration=${DURATION_S}s, tdc=${TDC_FREQ} Hz)"
echo "=================================================================="
echo "pcap     : $PCAP"
echo "listener : $LISTENER_JSON"
echo "logs     : $LOG_DIR/"
echo ""

if ! curl -s --max-time 2 http://localhost:8080/dashboard > /dev/null; then
    echo "ERROR: Serval not reachable on http://localhost:8080" >&2
    echo "Make sure the UI is running with --autostart-serval, or start Serval manually." >&2
    exit 1
fi
echo "[init] Serval reachable on :8080"

if ss -tln 2>/dev/null | grep -qE ':(7070|9090)\b'; then
    echo "ERROR: port 7070 or 9090 already in use:" >&2
    ss -tln 2>/dev/null | grep -E ':(7070|9090)\b' >&2
    echo "Is the UI mid-acquisition?  Stop it before running this script." >&2
    exit 1
fi
if ss -tln 2>/dev/null | grep -qE ':(5657|5658)\b'; then
    echo "ERROR: ZMQ port 5657 or 5658 already in use." >&2
    exit 1
fi
echo "[init] ports 7070/9090/5657/5658 are clear"
echo ""

echo "[1/5] tcpdump on lo: 'tcp port 7070 or tcp port 9090'"
"${TCPDUMP_PREFIX[@]}" "$TCPDUMP" -i lo -B 65536 -w "$PCAP" 'tcp port 7070 or tcp port 9090' \
    > "$LOG_DIR/tcpdump.log" 2>&1 &
TCPDUMP_PID=$!
sleep 0.7
if ! kill -0 "$TCPDUMP_PID" 2>/dev/null; then
    echo "ERROR: tcpdump exited immediately:" >&2
    cat "$LOG_DIR/tcpdump.log" >&2
    exit 1
fi
echo "      pid=$TCPDUMP_PID  pcap=$PCAP"
echo ""

echo "[2/5] splash_timepix.app on :9090, ZMQ :5657, heartbeat :5658"
"$PY" -m splash_timepix.app \
    --port 9090 \
    --zmq-port 5657 \
    --heartbeat-port 5658 \
    --tdc-frequency "$TDC_FREQ" \
    --flush-interval "$FLUSH_INTERVAL" \
    --collapse-y \
    > "$LOG_DIR/app.log" 2>&1 &
APP_PID=$!
for i in $(seq 1 30); do
    sleep 0.2
    if ss -tln 2>/dev/null | grep -qE ':9090\b'; then
        break
    fi
done
if ! ss -tln 2>/dev/null | grep -qE ':9090\b'; then
    echo "ERROR: app.py did not start listening on :9090 in 6s." >&2
    tail -40 "$LOG_DIR/app.log" >&2
    exit 1
fi
echo "      pid=$APP_PID listening on :9090"
echo ""

echo "[3/5] live-cli (--bin-width-exp ${LIVECLI_BIN_WIDTH_EXP} --max-delay-bins ${LIVECLI_MAX_DELAY_BINS} ${LIVECLI_EXTRA_ARGS})"
( cd "$(dirname "$LIVECLI")" && \
    "$LIVECLI" --bin-width-exp "$LIVECLI_BIN_WIDTH_EXP" --max-delay-bins "$LIVECLI_MAX_DELAY_BINS" $LIVECLI_EXTRA_ARGS \
        > "$LOG_DIR/livecli.log" 2>&1 ) &
LIVECLI_PID=$!
for i in $(seq 1 30); do
    sleep 0.2
    if ss -tln 2>/dev/null | grep -qE ':7070\b'; then
        break
    fi
done
if ! ss -tln 2>/dev/null | grep -qE ':7070\b'; then
    echo "ERROR: live-cli did not start listening on :7070 in 6s." >&2
    tail -40 "$LOG_DIR/livecli.log" >&2
    exit 1
fi
echo "      pid=$LIVECLI_PID listening on :7070, target=:9090"
echo ""

echo "[4/5] flush_pacing_listener.py → $LISTENER_JSON"
"$PY" scripts/sniffing/flush_pacing_listener.py \
    --zmq-port 5657 --hb-port 5658 \
    --ticker-interval 5.0 \
    --artifact "$LISTENER_JSON" \
    > "$LOG_DIR/listener.log" 2>&1 &
LISTENER_PID=$!
sleep 1.0
if ! kill -0 "$LISTENER_PID" 2>/dev/null; then
    echo "ERROR: listener exited immediately:" >&2
    cat "$LOG_DIR/listener.log" >&2
    exit 1
fi
echo "      pid=$LISTENER_PID"
echo ""

echo "[5/5] acq.py --preview -time ${DURATION_S}  (blocks until done)"
echo "      see $LOG_DIR/acq.log for live progress"
"$PY" -m splash_timepix.serval_client.acq \
    --preview -time "$DURATION_S" \
    > "$LOG_DIR/acq.log" 2>&1
ACQ_RC=$?
echo "      acq.py exit=${ACQ_RC}"
echo ""

echo "[drain] giving the pipeline 4 s to flush trailing data..."
sleep 4

echo ""
echo "=================================================================="
echo "TEAR DOWN + ANALYZE"
echo "=================================================================="
trap - EXIT
cleanup || true

echo ""
echo "Artifacts:"
echo "  pcap      : $PCAP   ($(du -h "$PCAP" 2>/dev/null | cut -f1))"
echo "  listener  : $LISTENER_JSON"
echo "  logs      : $LOG_DIR/"

if [ -f "$PCAP" ] && [ -f "$LISTENER_JSON" ]; then
    echo ""
    "$PY" scripts/sniffing/sniff_analyze.py "$PCAP" \
        --listener "$LISTENER_JSON" \
        --bin 1.0 --gap-threshold 2.0
fi
