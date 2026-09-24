#!/usr/bin/env bash
# Run every experiment end to end on Ubuntu and leave real data behind.
#
#   ./experiments/run_all.sh
#
# Produces:
#   results/*.csv        one row per Representative Operation
#   results/fig*.png     the report figures
#   results/run_all.log  every command's output, including the ping/tc checks
#   captures/*.pcap      the four captures the report screenshots come from
#
# Needs sudo (tc, tcpdump) and takes roughly 2-4 minutes.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD
PY=${PY:-$ROOT/.venv/bin/python}
PORT=${PORT:-5700}
FAIL_PORT=${FAIL_PORT:-5701}
RESULTS=$ROOT/results
CAPTURES=$ROOT/captures
LOG=$RESULTS/run_all.log
SERVER_PID=""

mkdir -p "$RESULTS" "$CAPTURES"
: > "$LOG"

say() { printf '\n\033[1m== %s\033[0m\n' "$*" | tee -a "$LOG"; }
run() { echo "\$ $*" >> "$LOG"; "$@" 2>&1 | tee -a "$LOG"; }

# ------------------------------------------------------------------ preflight
[ "$(uname -s)" = Linux ] || { echo "Parts 4-7 need Linux (tc/netem)."; exit 1; }
for tool in tc ping tcpdump; do
  command -v "$tool" >/dev/null || {
    echo "missing $tool -- sudo apt install iproute2 iputils-ping tcpdump"; exit 1; }
done
[ -x "$PY" ] || { echo "no interpreter at $PY (python3 -m venv .venv)"; exit 1; }
command -v tshark >/dev/null || echo "note: tshark not installed; retransmissions must be counted in the Wireshark GUI"
echo "caching sudo credentials for tc/tcpdump..."
sudo -v

cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  sudo pkill -INT -f "tcpdump -i lo" 2>/dev/null || true
  sudo tc qdisc del dev lo root 2>/dev/null || true
}
trap cleanup EXIT

wait_port() {
  for _ in $(seq 1 60); do
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&-; return 0; }
    sleep 0.1
  done
  echo "port $1 never opened"; exit 1
}

start_server() {
  "$PY" src/server.py --port "$PORT" --log-level WARNING >>"$RESULTS/server.log" 2>&1 &
  SERVER_PID=$!
  wait_port "$PORT"
}

cap_start() {  # $1 = filename, $2 = port
  # -U writes every packet straight to the file.  Without it tcpdump buffers,
  # and a workload that finishes in milliseconds is still sitting in the buffer
  # when we stop the capture -- which yields a 24-byte, header-only pcap.
  local stderr="/tmp/tcpdump-$1.err"
  : > "$stderr"
  sudo tcpdump -i lo -s 0 -U "port $2" -w "$CAPTURES/$1" 2>"$stderr" &
  for _ in $(seq 1 100); do      # wait for the filter to be attached, not a guess
    grep -q "listening on" "$stderr" && { sleep 0.3; return 0; }
    sleep 0.1
  done
  echo "WARNING: tcpdump never started for $1" | tee -a "$LOG"
}

cap_stop() {   # $1 = filename
  sleep 0.5                      # let the last packets of the workload arrive
  sudo pkill -INT -f "tcpdump -i lo" 2>/dev/null || true
  sleep 0.5
  sudo chown "$(id -un):$(id -gn)" "$CAPTURES/$1" 2>/dev/null || true
  cat "/tmp/tcpdump-$1.err" >> "$LOG" 2>/dev/null || true
  local n
  n=$(tcpdump -r "$CAPTURES/$1" 2>/dev/null | wc -l | tr -d ' ')
  printf '  captured %s packets into %s\n' "$n" "$1" | tee -a "$LOG"
  [ "$n" -gt 0 ] || echo "  WARNING: $1 is empty" | tee -a "$LOG"
}

netem() { run "$ROOT/experiments/netem.sh" "$@"; }
verify() { echo "\$ ping -c 20 127.0.0.1" >> "$LOG"; ping -c 20 127.0.0.1 | tail -3 | tee -a "$LOG"; }

: > "$RESULTS/server.log"
start_server
say "server up on 127.0.0.1:$PORT (pid $SERVER_PID)"

# ------------------------------------------------- Experiment 1: 100ms latency
say "Experiment 1 -- baseline"
netem clear
verify
run "$PY" experiments/bench.py ro --n 30 --port "$PORT" --label baseline-latency

say "Experiment 1 -- netem delay 100ms"
netem delay 100ms
verify                           # expect ~200 ms RTT: lo is crossed in both directions
run "$PY" experiments/bench.py ro --n 30 --port "$PORT" --label latency-100ms
netem clear

# ---------------------------------------------- Experiment 2: 5% packet loss
say "Experiment 2 -- baseline (100 ROs, capturing)"
netem clear
cap_start baseline-loss.pcap "$PORT"
run "$PY" experiments/bench.py ro --n 100 --port "$PORT" --label baseline-loss
cap_stop baseline-loss.pcap

say "Experiment 2 -- netem loss 5% (100 ROs, capturing)"
netem loss 5%
cap_start loss-5pct.pcap "$PORT"
run "$PY" experiments/bench.py ro --n 100 --port "$PORT" --label loss-5pct
cap_stop loss-5pct.pcap
netem clear

# --------------------------------------------- Experiment 3: concurrent clients
say "Experiment 3 -- 1, 3, 5 concurrent clients x 20 ROs"
for n in 1 3 5; do
  run "$PY" experiments/bench.py concurrency --clients "$n" --ops 20 --port "$PORT" --label "c$n"
done

# ----------------------------------------------------- Part 6: Network Mystery
say "Part 6 -- Network Mystery (framing bug), capturing"
cap_start mystery.pcap "$PORT"
run "$PY" mystery/buggy_client.py --port "$PORT" --n 4 || true   # expected to fail
run "$PY" mystery/buggy_client.py --port "$PORT" --n 4 --fixed
cap_stop mystery.pcap

kill "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""

# ------------------------------- Experiment 4 + Part 7: failure and improvement
# exp_failure.py starts and stops its own server on $FAIL_PORT.
say "Experiment 4 -- server failure, BEFORE (no timeout, no retry), capturing"
cap_start failure.pcap "$FAIL_PORT"
run "$PY" experiments/exp_failure.py --mode baseline --failure kill \
      --port "$FAIL_PORT" --n 20 --fail-after 5 --downtime 3
cap_stop failure.pcap

say "Part 7 -- same workload, AFTER (timeout + backoff reconnect)"
run "$PY" experiments/exp_failure.py --mode resilient --failure kill \
      --port "$FAIL_PORT" --n 20 --fail-after 5 --downtime 3

say "Part 7 -- frozen server (SIGSTOP): only a timeout can detect this"
run "$PY" experiments/exp_failure.py --mode resilient --failure freeze \
      --port "$FAIL_PORT" --n 10 --fail-after 4 --downtime 8 --timeout 2

# ------------------------------------------------------------------- analysis
say "TCP retransmissions"
if command -v tshark >/dev/null; then
  for f in baseline-loss loss-5pct failure mystery; do
    [ -s "$CAPTURES/$f.pcap" ] || continue
    # An empty or truncated capture makes tshark exit non-zero; with pipefail
    # that would take the whole script down, so swallow it explicitly.
    n=$(tshark -r "$CAPTURES/$f.pcap" -Y tcp.analysis.retransmission 2>/dev/null | wc -l || true)
    printf '  %-16s %s retransmissions\n' "$f.pcap" "${n:-?}" | tee -a "$LOG"
  done
else
  echo "  open each capture in Wireshark and apply: tcp.analysis.retransmission" | tee -a "$LOG"
fi

say "figures"
run "$PY" experiments/plot.py

say "done"
netem show
ls -la "$RESULTS" "$CAPTURES" | tee -a "$LOG"
cat <<'EOF'

Next, by hand (a script cannot take the screenshots):
  wireshark captures/baseline-loss.pcap   # handshake + one RO         -> Part 5 event 1
  wireshark captures/loss-5pct.pcap       # tcp.analysis.retransmission -> Part 5 event 2
  wireshark captures/failure.pcap         # FIN / RST at the outage     -> Part 5 event 3
  wireshark captures/mystery.pcap         # two replies in one segment  -> Part 6 evidence
EOF
