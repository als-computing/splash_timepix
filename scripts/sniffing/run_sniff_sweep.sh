#!/usr/bin/env bash
# Sweep live-cli's sort-buffer knobs and compare bolus sizes.
#
# Hypothesis: if the 25 MiB bolus is the sort-stage buffer, it'll scale
# with --max-delay-bins.  If it stays ~25 MiB regardless, the buffer is
# downstream of sort and the sort knobs don't matter.
#
# Per-run summary line format (grep-friendly):
#   RESULT idx=N/9 tag=TAG bwe=X mdb=Y status=OK|CRASH bolus=BYTES n_pkts_A=N n_pkts_B=M

set -uo pipefail

DURATION_S="${DURATION_S:-60}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"
PY="$PROJECT_ROOT/.venv/bin/python"

# 3x3 grid: bwe ∈ {0, 10, 100}  ×  mdb ∈ {1, 10, 100}
configs=()
for bwe in 0 10 100; do
    for mdb in 1 10 100; do
        configs+=("bwe${bwe}_mdb${mdb}|${bwe}|${mdb}")
    done
done

n_total="${#configs[@]}"

echo "================================================================"
echo "LIVE-CLI KNOB SWEEP  ($n_total runs × ${DURATION_S}s each)"
echo "Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"

idx=0
for cfg in "${configs[@]}"; do
    idx=$((idx + 1))
    IFS='|' read -r tag bwe mdb <<< "$cfg"
    started_at=$(date '+%H:%M:%S')
    echo ""
    echo "----------------------------------------------------------------"
    echo "[$idx/$n_total]  $started_at  tag=$tag  bwe=$bwe  mdb=$mdb"
    echo "----------------------------------------------------------------"

    LIVECLI_BIN_WIDTH_EXP="$bwe" \
    LIVECLI_MAX_DELAY_BINS="$mdb" \
    LIVECLI_EXTRA_ARGS="" \
    DURATION_S="$DURATION_S" \
    TAG="$tag" \
    bash "$PROJECT_ROOT/scripts/sniffing/run_sniff_experiment.sh" 2>&1 | \
        grep -E '^\[(init|1/5|2/5|3/5|4/5|5/5|drain|cleanup)\]|ERROR|live-cli|panic' || true

    pcap=$(ls -t /tmp/sniff_*_${tag}.pcap 2>/dev/null | head -1)
    log_dir=$(ls -td /tmp/sniff_logs_*_${tag} 2>/dev/null | head -1)
    livecli_log="$log_dir/livecli.log"

    status="OK"
    if [ -f "$livecli_log" ] && grep -q "panicked at" "$livecli_log"; then
        status="CRASH"
    fi

    bolus=0; n_a=0; n_b=0; total_b_bytes=0
    if [ -n "$pcap" ] && [ -f "$pcap" ]; then
        eval "$("$PY" - "$pcap" <<'PYEOF'
import sys, dpkt
pcap = sys.argv[1]
hop_a = hop_b = 0
bolus = 0
n_a = n_b = 0
with open(pcap, "rb") as f:
    r = dpkt.pcap.Reader(f)
    cur = 0
    last_ts = None
    for ts, buf in r:
        try:
            ip = dpkt.ethernet.Ethernet(buf).data
            if not isinstance(ip, dpkt.ip.IP): continue
            tcp = ip.data
            if not isinstance(tcp, dpkt.tcp.TCP): continue
            payload = len(tcp.data)
            if payload <= 0: continue
            if tcp.dport == 7070:
                hop_a += payload
                n_a += 1
            elif tcp.dport == 9090:
                hop_b += payload
                n_b += 1
                # cluster B-side packets within 0.5s into a single "bolus"
                if last_ts is None or (ts - last_ts) < 0.5:
                    cur += payload
                else:
                    if cur > bolus:
                        bolus = cur
                    cur = payload
                last_ts = ts
        except Exception:
            continue
    if cur > bolus:
        bolus = cur
print(f"bolus={bolus}; n_a={n_a}; n_b={n_b}; total_b_bytes={hop_b}")
PYEOF
)"
    fi

    finished_at=$(date '+%H:%M:%S')
    echo ""
    printf "RESULT idx=%d/%d tag=%s bwe=%s mdb=%s status=%s bolus=%d n_pkts_A=%d n_pkts_B=%d total_B_bytes=%d started=%s finished=%s\n" \
        "$idx" "$n_total" "$tag" "$bwe" "$mdb" "$status" "$bolus" "$n_a" "$n_b" "$total_b_bytes" "$started_at" "$finished_at"
    echo ""

    sleep 2
done

echo "================================================================"
echo "ALL DONE: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"
echo ""
echo "Summary table:"
grep -E '^RESULT idx=' /dev/stdin 2>/dev/null || true

echo ""
echo "Re-summarizing all RESULT lines from this run:"
echo ""
echo "(extracted from terminal output above)"
echo ""
"$PY" - <<'PYEOF'
import re, glob, os
# Re-build summary by inspecting all artifacts saved with our tag pattern
print(f"{'tag':<14} {'bwe':>4} {'mdb':>4} {'status':>7} {'bolus':>12} {'#pkts B':>8}")
print("-" * 60)
import dpkt
results = []
for pcap in sorted(glob.glob("/tmp/sniff_*_bwe*_mdb*.pcap"), key=os.path.getmtime):
    base = os.path.basename(pcap)
    m = re.match(r"sniff_\d+_(bwe(\d+)_mdb(\d+))\.pcap$", base)
    if not m: continue
    tag, bwe, mdb = m.group(1), m.group(2), m.group(3)
    bolus = 0; n_b = 0; cur = 0; last_ts = None
    with open(pcap, "rb") as f:
        r = dpkt.pcap.Reader(f)
        for ts, buf in r:
            try:
                ip = dpkt.ethernet.Ethernet(buf).data
                if not isinstance(ip, dpkt.ip.IP): continue
                tcp = ip.data
                if not isinstance(tcp, dpkt.tcp.TCP): continue
                payload = len(tcp.data)
                if payload <= 0 or tcp.dport != 9090: continue
                n_b += 1
                if last_ts is None or (ts - last_ts) < 0.5:
                    cur += payload
                else:
                    bolus = max(bolus, cur)
                    cur = payload
                last_ts = ts
            except Exception:
                continue
        bolus = max(bolus, cur)
    log_dir_glob = f"/tmp/sniff_logs_*_{tag}"
    log_dirs = sorted(glob.glob(log_dir_glob), key=os.path.getmtime)
    status = "OK"
    if log_dirs:
        livecli = os.path.join(log_dirs[-1], "livecli.log")
        try:
            with open(livecli) as lf:
                if "panicked at" in lf.read():
                    status = "CRASH"
        except FileNotFoundError:
            pass
    results.append((tag, bwe, mdb, status, bolus, n_b))
    print(f"{tag:<14} {bwe:>4} {mdb:>4} {status:>7} {bolus:>12,} {n_b:>8}")
PYEOF
