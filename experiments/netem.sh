#!/usr/bin/env bash
# tc/netem helper for the loopback interface (Ubuntu; needs sudo).
#
#   ./experiments/netem.sh clear          # remove any impairment
#   ./experiments/netem.sh delay 100ms    # Experiment 1
#   ./experiments/netem.sh loss 5%        # Experiment 2
#   ./experiments/netem.sh show           # verify what is configured
#   ./experiments/netem.sh verify         # ping -c 20 127.0.0.1
#
# Note: netem is attached to the egress qdisc of lo, and loopback traffic
# crosses lo in both directions, so "delay 100ms" shows up as ~200 ms of RTT.
set -euo pipefail

DEV=lo
cmd=${1:-show}

case "$cmd" in
  clear)  sudo tc qdisc del dev "$DEV" root 2>/dev/null || echo "no qdisc to delete"; ;;
  delay)  sudo tc qdisc replace dev "$DEV" root netem delay "${2:-100ms}" ;;
  loss)   sudo tc qdisc replace dev "$DEV" root netem loss  "${2:-5%}" ;;
  show)   tc qdisc show dev "$DEV" ;;
  verify) ping -c 20 127.0.0.1 | tail -n 3 ;;
  *) echo "usage: $0 {clear|delay <t>|loss <p>|show|verify}" >&2; exit 1 ;;
esac

[ "$cmd" = show ] || tc qdisc show dev "$DEV"
