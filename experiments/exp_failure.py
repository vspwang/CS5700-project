"""Experiment 4 (server failure) and the Part 7 before/after comparison.

The script owns the server process so the whole outage is reproducible with one
command.  A client performs --n Representative Operations; partway through, the
server is killed (or frozen), then brought back --downtime seconds later.

    # BEFORE: no timeout, no retry
    python experiments/exp_failure.py --mode baseline  --failure kill
    # AFTER: timeout + exponential-backoff reconnect
    python experiments/exp_failure.py --mode resilient --failure kill
    # why the timeout matters: a frozen server never sends FIN
    python experiments/exp_failure.py --mode resilient --failure freeze

--failure kill   SIGTERM: the kernel closes the socket, so the client sees EOF
                 or ECONNRESET almost immediately.
--failure freeze SIGSTOP: the socket stays open and silent, so only a timeout
                 can detect the failure.
"""

import argparse
import csv
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from client import VoteClient, ServerError  # noqa: E402

RESULTS = ROOT / "results"


def start_server(port):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "src" / "server.py"),
         "--port", str(port), "--log-level", "WARNING"])
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError(f"server did not come up on port {port}")


def main():
    ap = argparse.ArgumentParser(description="server-failure experiment")
    ap.add_argument("--mode", choices=["baseline", "resilient"], default="baseline")
    ap.add_argument("--failure", choices=["kill", "freeze"], default="kill")
    ap.add_argument("--port", type=int, default=5701)
    ap.add_argument("--n", type=int, default=20, help="Representative Operations")
    ap.add_argument("--fail-after", type=int, default=5, help="break the server after this many ROs")
    ap.add_argument("--downtime", type=float, default=3.0, help="seconds of outage")
    ap.add_argument("--timeout", type=float, default=5.0, help="resilient socket timeout (s)")
    ap.add_argument("--baseline-cap", type=float, default=60.0,
                    help="safety cap so the baseline run terminates; a result at "
                         "the cap means the baseline never detected the failure")
    ap.add_argument("--option", default="B")
    a = ap.parse_args()

    resilient = a.mode == "resilient"
    proc = start_server(a.port)
    client = VoteClient("127.0.0.1", a.port,
                        timeout=a.timeout if resilient else a.baseline_cap,
                        resilient=resilient)
    client.connect()

    voters = [f"fail-{i:06d}" for i in range(a.n)]
    for v in voters:                                  # setup, before the outage
        client.register(v)

    restore_at = [None]

    def break_server():
        if a.failure == "kill":
            proc.terminate()
            print(f"[{time.strftime('%H:%M:%S')}] server terminated (SIGTERM)")
        else:
            os.kill(proc.pid, signal.SIGSTOP)
            print(f"[{time.strftime('%H:%M:%S')}] server frozen (SIGSTOP)")
        restore_at[0] = time.perf_counter() + a.downtime

    def restore_server():
        nonlocal proc
        if a.failure == "kill":
            proc.wait()
            proc = start_server(a.port)
            print(f"[{time.strftime('%H:%M:%S')}] server restarted")
        else:
            os.kill(proc.pid, signal.SIGCONT)
            print(f"[{time.strftime('%H:%M:%S')}] server resumed (SIGCONT)")

    rows, timer = [], None
    try:
        for i, voter in enumerate(voters):
            if i == a.fail_after:
                break_server()
                timer = threading.Timer(a.downtime, restore_server)
                timer.start()
            t0 = time.perf_counter()
            try:
                client.vote(voter, a.option)
                err = ""
            except ServerError as e:
                err = e.code
            except Exception as e:
                err = type(e).__name__
            dt = (time.perf_counter() - t0) * 1000
            rows.append([i, voter, round(dt, 3), int(not err), err])
            print(f"op {i:>3}  {'OK ' if not err else 'FAIL'}  {dt:8.1f} ms  {err}")
    finally:
        if timer:
            timer.join()
        client.close()
        if proc.poll() is None:
            if a.failure == "freeze":
                os.kill(proc.pid, signal.SIGCONT)
            proc.terminate()
            proc.wait()

    label = f"failure-{a.mode}-{a.failure}"
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / f"{label}.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["iter", "voter_id", "elapsed_ms", "ok", "err"])
        w.writerows(rows)

    ok = sum(r[3] for r in rows)
    after = [r for r in rows if r[0] >= a.fail_after]
    first_fail = next((r for r in after if not r[3]), None)
    print(f"\n--- {label} ---")
    print(f"completed operations      : {ok}/{len(rows)}")
    print(f"completed after the fault : {sum(r[3] for r in after)}/{len(after)}")
    if first_fail:
        detect = first_fail[2]
        capped = detect >= a.baseline_cap * 1000 * 0.95
        print(f"failure detection time    : {detect:.1f} ms"
              + ("  (hit the safety cap: never detected)" if capped else ""))
    else:
        print("failure detection time    : n/a (no operation ever failed)")
    print(f"client reconnects         : {client.reconnects}")
    print(f"wrote results/{label}.csv")


if __name__ == "__main__":
    main()
