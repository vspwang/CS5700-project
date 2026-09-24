"""Measurement harness for Experiments 1-3.

Representative Operation (RO): send one VOTE request on an already-open
connection and read the complete VOTE_ACK response.  Timing starts just before
sendall() and stops only after read_frame() has assembled the whole reply, so
the measurement follows VPP/1 framing rather than a single recv().

Every RO is cast by a *distinct* pre-registered voter (bench-000001, ...), so
one-vote-per-voter stays intact while all 30/100 executions hit the identical
server code path.  Registration happens before the timer starts.

    python experiments/bench.py ro          --n 30  --label baseline-latency
    python experiments/bench.py concurrency --clients 5 --ops 20 --label c5
"""

import argparse
import csv
import multiprocessing as mp
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from client import VoteClient, ServerError  # noqa: E402

RESULTS = ROOT / "results"


def summarize(label, rtts, wall, ok, failed):
    rtts = sorted(rtts)
    row = {
        "label": label,
        "n": ok + failed,
        "ok": ok,
        "failed": failed,
        "mean_ms": round(statistics.fmean(rtts), 3) if rtts else 0.0,
        "median_ms": round(statistics.median(rtts), 3) if rtts else 0.0,
        "p95_ms": round(rtts[min(len(rtts) - 1, int(0.95 * len(rtts)))], 3) if rtts else 0.0,
        "min_ms": round(rtts[0], 3) if rtts else 0.0,
        "max_ms": round(rtts[-1], 3) if rtts else 0.0,
        "wall_s": round(wall, 3),
        "ops_per_s": round((ok / wall) if wall else 0.0, 1),
    }
    width = max(len(k) for k in row)
    print(f"\n--- {label} ---")
    for k, v in row.items():
        print(f"{k:>{width}} : {v}")
    return row


def write_csv(path, rows, header):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(f"wrote {path.relative_to(ROOT)}")


def run_ro(host, port, poll_id, n, prefix, option, client=None):
    """n Representative Operations on one connection.

    Returns (rows, t_start, t_end).  The span covers only the measured loop, so
    connection setup and voter registration never count towards throughput.
    Wall-clock time.time() is used so spans from separate processes can be
    combined by the parent.
    """
    own = client is None
    if own:
        client = VoteClient(host, port, poll_id).connect()

    voters = [f"{prefix}{i:06d}" for i in range(n)]
    for v in voters:                       # setup: not part of any measurement
        client.register(v)

    rows = []
    t_start = time.time()
    for i, voter in enumerate(voters):
        t0 = time.perf_counter()
        try:
            client.vote(voter, option)
            err = ""
        except ServerError as e:
            err = e.code
        except OSError as e:
            err = type(e).__name__
        t1 = time.perf_counter()
        rows.append([i, voter, round((t1 - t0) * 1000, 4), int(not err), err])
    t_end = time.time()

    if own:
        client.close()
    return rows, t_start, t_end


def _worker(args):
    host, port, poll_id, ops, prefix, option = args
    return run_ro(host, port, poll_id, ops, prefix, option)


def cmd_ro(a):
    client = VoteClient(a.host, a.port, a.poll_id).connect()
    client.reset(a.admin_key)
    rows, t0, t1 = run_ro(a.host, a.port, a.poll_id, a.n, "bench-", a.option,
                          client=client)
    wall = t1 - t0
    client.close()

    write_csv(RESULTS / f"{a.label}.csv", rows,
              ["iter", "voter_id", "rtt_ms", "ok", "err"])
    ok = sum(r[3] for r in rows)
    summarize(a.label, [r[2] for r in rows if r[3]], wall, ok, len(rows) - ok)


def cmd_concurrency(a):
    admin = VoteClient(a.host, a.port, a.poll_id).connect()
    admin.reset(a.admin_key)
    admin.close()

    jobs = [(a.host, a.port, a.poll_id, a.ops, f"c{i}-", a.option)
            for i in range(a.clients)]
    with mp.Pool(a.clients) as pool:
        per_client = pool.map(_worker, jobs)
    # Throughput covers the window in which the clients were actually running,
    # not process startup or registration.
    wall = max(t1 for _, _, t1 in per_client) - min(t0 for _, t0, _ in per_client)

    rows = [[i] + r for i, (client_rows, _, _) in enumerate(per_client)
            for r in client_rows]
    write_csv(RESULTS / f"{a.label}.csv", rows,
              ["client", "iter", "voter_id", "rtt_ms", "ok", "err"])
    ok = sum(r[4] for r in rows)
    summarize(a.label, [r[3] for r in rows if r[4]], wall, ok, len(rows) - ok)


def main():
    ap = argparse.ArgumentParser(description="Polly measurement harness")
    ap.add_argument("mode", choices=["ro", "concurrency"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5700)
    ap.add_argument("--poll-id", default="p-demo")
    ap.add_argument("--option", default="B")
    ap.add_argument("--admin-key", default="cs5700-admin")
    ap.add_argument("--n", type=int, default=30, help="ROs in 'ro' mode")
    ap.add_argument("--clients", type=int, default=5)
    ap.add_argument("--ops", type=int, default=20, help="ROs per client")
    ap.add_argument("--label", default="run")
    a = ap.parse_args()
    (cmd_ro if a.mode == "ro" else cmd_concurrency)(a)


if __name__ == "__main__":
    main()
