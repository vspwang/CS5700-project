# Polly — a TCP voting/polling system (CS 5700 sample solution)

A small client/server polling application over TCP, plus the measurement,
troubleshooting, and resilience work required by Parts 1–7 of the individual
project.

* **Application:** distributed voting / polling
* **Protocol:** VPP/1 — length-prefixed JSON over TCP (`src/protocol.py`)
* **Operations:** `REGISTER`, `VOTE`, `RESULTS` (plus `RESET` for repeatable experiments)
* **Concurrency:** thread-per-connection, shared state behind one mutex
* **Language:** Python 3.8+ (standard library only; `matplotlib` is needed only for the figures)

---

## Setup

```bash
git clone <this repo> && cd CS5700-project
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # pytest + matplotlib; the app itself needs neither

# Ubuntu, for Parts 4–6:
sudo apt update && sudo apt install iproute2 iputils-ping tcpdump tshark
```

Development runs anywhere; **Parts 4–7 must be run on Ubuntu** because they need
`tc`/`netem`, `tcpdump`, and `ss`.

## Running it

```bash
# terminal 1
python src/server.py --port 5700

# terminal 2
python src/client.py register --voter alice
python src/client.py vote     --voter alice --option B
python src/client.py results
python src/client.py reset                    # clears the ballot box
```

Five simultaneous clients:

```bash
for i in 1 2 3 4 5; do python experiments/bench.py concurrency --clients 1 --ops 20 --label manual-$i & done; wait
```

Tests:

```bash
python -m pytest tests -q        # 37 tests: framing, operations, errors, concurrency, resilience
```

---

## Protocol summary (VPP/1)

### Framing

```
+--------+--------+--------+--------+---------- ... ----------+
|       length N (uint32, big endian) |    N bytes of UTF-8 JSON    |
+--------+--------+--------+--------+---------- ... ----------+
```

TCP is a byte stream, so one `recv()` may return half a message, one message, or
three. A receiver must read **exactly** 4 header bytes, decode `N`, then read
**exactly** `N` body bytes, looping until it has them
(`protocol.recv_exact` / `protocol.read_frame`). `N` is capped at 64 KiB.

### Envelope

```json
{"v": 1, "t": "VOTE", "id": "c1a2b-000007", "p": {"voter_id": "alice", "poll_id": "p-demo", "option_id": "B", "token": "1dc8a724f3ff6f38"}}
```

`v` protocol version · `t` message type · `id` client-generated request id,
echoed in the response so replies can be matched to requests · `p` payload.

### Messages

| Message | Direction | Payload | Meaning |
|---|---|---|---|
| `REGISTER` | C→S | `voter_id` | Register a voter; idempotent |
| `REGISTER_OK` | S→C | `voter_id, token, already` | Issues the voter's token |
| `VOTE` | C→S | `voter_id, poll_id, option_id, token` | Cast one ballot (**the Representative Operation**) |
| `VOTE_ACK` | S→C | `poll_id, option_id, seq` | Ballot accepted; `seq` is its serial number |
| `RESULTS` | C→S | `poll_id` | Ask for the current tally |
| `RESULTS_OK` | S→C | `poll_id, title, options, tallies, total, registered` | Current tally |
| `RESET` | C→S | `admin_key` | Clear the ballot box (experiment harness only) |
| `RESET_OK` | S→C | `poll_id` | Ballot box cleared |
| `ERROR` | S→C | `code, msg` | Request rejected |

Error codes: `BAD_FRAME, BAD_JSON, UNSUPPORTED_VERSION, MSG_TOO_LARGE,
UNKNOWN_TYPE, MISSING_FIELD, UNKNOWN_POLL, UNKNOWN_OPTION, BAD_TOKEN,
DUPLICATE_VOTE, NOT_AUTHORIZED, INTERNAL`.

### Connection handling

* **Establishment** — no application-layer handshake; the server never speaks
  first, so a client's first inbound frame is always its own response.
* **Termination** — the client closes; the server sees a clean EOF on a frame
  boundary, tears down the thread, and closes.
* **Errors come in two kinds.** A *semantic* error (unknown type, missing field,
  duplicate vote) consumed a whole well-formed frame, so the byte stream is
  still aligned: the server replies `ERROR` and **keeps the connection open**. A
  *framing* error (impossible length, non-UTF-8 body, non-JSON body) means the
  next frame boundary is unknowable: the server replies `ERROR` once and
  **closes**.
* **Tokens are stateless** — `HMAC-SHA256(secret, voter_id)`, truncated. Nothing
  about a voter is stored in a session table, so a token stays valid across a
  server restart. That is what lets the resilient client reconnect and keep
  voting without re-registering.

---

## Representative Operation (RO)

> **One `VOTE` request sent on an already-open connection, timed until the
> complete `VOTE_ACK` response has been read.**

Timing starts immediately before `sendall()` and stops only after
`read_frame()` has assembled the whole reply — it follows the framing rules, not
a single `recv()`.

It represents normal use because voting is the application's only high-frequency
operation and one `VOTE` exercises the entire main path: token check → poll
check → duplicate check → tally update → acknowledgment. `REGISTER` happens once
per voter for a lifetime and `RESULTS` is a low-frequency read.

**Keeping the workload constant.** Every RO is cast by a *different*
pre-registered voter with a fixed-width id (`bench-000001`, …), so the request
and response are the same size every time and every execution takes the identical
server code path (a first vote), without weakening one-vote-per-voter.
Registration runs before the timer starts and is never measured. The connection
is opened once and reused, so an RO measures one round trip and not a TCP
handshake.

---

## Reproducing the experiments

All four run on `127.0.0.1`. `experiments/netem.sh` wraps the `tc` commands.

### Experiment 1 — induced latency (30 ROs per condition)

```bash
./experiments/netem.sh clear
ping -c 20 127.0.0.1                                          # verify baseline
python experiments/bench.py ro --n 30 --label baseline-latency

./experiments/netem.sh delay 100ms                            # sudo tc qdisc replace dev lo root netem delay 100ms
ping -c 20 127.0.0.1                                          # verify: ~200 ms RTT, see note below
python experiments/bench.py ro --n 30 --label latency-100ms

./experiments/netem.sh clear
```

> netem sits on the **egress** qdisc of `lo`, and loopback traffic crosses `lo`
> in both directions, so a configured `delay 100ms` shows up as roughly **200 ms**
> of RTT. Measure and report what you actually observe.

### Experiment 2 — 5% packet loss (100 ROs per condition)

```bash
./experiments/netem.sh clear
sudo tcpdump -i lo port 5700 -w captures/baseline-loss.pcap &
python experiments/bench.py ro --n 100 --label baseline-loss
sudo pkill tcpdump

./experiments/netem.sh loss 5%
tc qdisc show dev lo                                          # confirm
sudo tcpdump -i lo port 5700 -w captures/loss-5pct.pcap &
python experiments/bench.py ro --n 100 --label loss-5pct
sudo pkill tcpdump

./experiments/netem.sh clear

# retransmission counts
tshark -r captures/baseline-loss.pcap -Y tcp.analysis.retransmission | wc -l
tshark -r captures/loss-5pct.pcap     -Y tcp.analysis.retransmission | wc -l
```

The 100-execution workload (not a large transfer) is the right choice here
because the RO is a small request/response, not a bulk transfer.

### Experiment 3 — concurrent clients (20 ROs per client)

```bash
for n in 1 3 5; do python experiments/bench.py concurrency --clients $n --ops 20 --label c$n; done
```

Each client is a separate OS process with its own connection and its own voter
id prefix, so per-client work is identical and only the client count varies.

### Experiment 4 — server failure

```bash
sudo tcpdump -i lo port 5701 -w captures/failure.pcap &
python experiments/exp_failure.py --mode baseline  --failure kill
sudo pkill tcpdump
```

The script starts the server itself, runs 20 ROs, terminates the server after
the 5th, restarts it 3 s later, and reports what the client saw.

### Figures

```bash
python experiments/plot.py      # writes results/fig1..fig3 from whatever CSVs exist
```

Outputs land in `results/` (one CSV per condition, `rtt_ms` per operation) and
`captures/`.

---

## Network Mystery (Part 6)

```bash
python mystery/buggy_client.py            # fails
python mystery/buggy_client.py --fixed    # succeeds
```

A client that reads with `data = sock.recv(4096)` and `json.loads(data[4:])` —
i.e. one that assumes one `recv()` is one application message and ignores the
length prefix. `mystery/buggy_client.py` reproduces the **coalescing** half
deterministically (several replies land in one `recv()`); adding
`./experiments/netem.sh delay 100ms` and a larger response reproduces the
**splitting** half. The header the client itself prints is the whole diagnosis:

```
recv() returned 364 bytes (header says 87)
FAILED: JSONDecodeError: Extra data: line 1 column 88 (char 87)
```

Capture it with `sudo tcpdump -i lo port 5700 -w captures/mystery.pcap`.

## System improvement (Part 7)

Baseline client: no socket timeout, no retry. Resilient client: socket timeout +
bounded exponential-backoff reconnect, replaying the failed request **with the
same request id**. One flag selects between them, so before/after differ in
exactly one variable:

```bash
python experiments/exp_failure.py --mode baseline  --failure kill    # BEFORE
python experiments/exp_failure.py --mode resilient --failure kill    # AFTER

# why a timeout is needed as well as reconnection: a frozen server (SIGSTOP)
# never sends FIN, so nothing but a timeout can detect it
python experiments/exp_failure.py --mode resilient --failure freeze
```

A replayed `VOTE` cannot double-count: one-vote-per-voter means the retry either
succeeds (the original never landed) or returns `DUPLICATE_VOTE` (the original
did land), and the client treats the latter as success.

---

## Layout

```
src/protocol.py           framing, envelope, error codes
src/server.py             PollStore + thread-per-connection VoteServer
src/client.py             VoteClient (baseline and resilient) + CLI
experiments/netem.sh      tc/netem helper
experiments/bench.py      Experiments 1–3
experiments/exp_failure.py Experiment 4 + Part 7 before/after
experiments/plot.py       figures
mystery/buggy_client.py   Part 6
tests/                    pytest suite
captures/                 .pcap files referenced by the report
results/                  CSVs and figures (regenerate on Ubuntu)
```
