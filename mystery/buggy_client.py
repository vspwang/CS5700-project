"""Part 6 -- Network Mystery: a client that ignores VPP/1 framing.

The bug is two lines:

    data = sock.recv(4096)      # assumes one recv() == one application message
    msg  = json.loads(data[4:]) # ignores the 4-byte length prefix

VPP/1 says "read exactly 4 header bytes, then exactly N body bytes".  This
client instead trusts recv() to hand back message-shaped chunks.  TCP makes no
such promise, and the mistake shows up in both directions:

  * COALESCING (reproduced here, deterministically): the client pipelines N
    VOTE requests, waits, then issues one recv() per expected reply.  The first
    recv() returns *all* the replies concatenated, so json.loads() raises
    "Extra data: line 1 column ...".
  * SPLITTING (reproduce with `experiments/netem.sh delay 100ms` and a larger
    response): one application message arrives in two TCP segments, the first
    recv() returns half of it, and json.loads() raises "Unterminated string".

Run both halves for the report:

    python mystery/buggy_client.py            # broken: fails
    python mystery/buggy_client.py --fixed    # read_frame(): succeeds

Capture it with:  sudo tcpdump -i lo port 5700 -w captures/mystery.pcap
"""

import argparse
import json
import random
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import protocol  # noqa: E402
from client import VoteClient  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Network Mystery: framing bug")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5700)
    ap.add_argument("--poll-id", default="p-demo")
    ap.add_argument("--option", default="B")
    ap.add_argument("--n", type=int, default=4, help="requests to pipeline")
    ap.add_argument("--settle", type=float, default=0.05,
                    help="seconds to let replies accumulate before reading")
    ap.add_argument("--fixed", action="store_true",
                    help="use protocol.read_frame() instead of the buggy reader")
    a = ap.parse_args()

    # Setup over a correct client, so the failure below is unambiguously
    # about reading, not about registration.  Fresh voter ids each run keeps
    # repeated demos from tripping over one-vote-per-voter.
    tag = f"{random.randrange(1 << 16):04x}"
    voters = [f"mystery-{tag}-{i:04d}" for i in range(a.n)]
    setup = VoteClient(a.host, a.port, a.poll_id).connect()
    tokens = {v: setup.register(v)["token"] for v in voters}
    setup.close()

    sock = socket.create_connection((a.host, a.port), timeout=5)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    for i, voter in enumerate(voters):
        sock.sendall(protocol.encode("VOTE", f"m-{i:04d}", {
            "voter_id": voter, "poll_id": a.poll_id,
            "option_id": a.option, "token": tokens[voter]}))
    print(f"pipelined {a.n} VOTE requests; waiting {a.settle}s for the replies")
    time.sleep(a.settle)

    try:
        for i in range(a.n):
            if a.fixed:
                msg = protocol.read_frame(sock)
            else:
                data = sock.recv(4096)            # BUG
                print(f"  recv() returned {len(data)} bytes "
                      f"(header says {int.from_bytes(data[:4], 'big')})")
                msg = json.loads(data[4:])        # BUG
            print(f"  reply {i}: {msg['t']} id={msg['id']}")
    except json.JSONDecodeError as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        print("The bytes arrived intact -- check Wireshark. The client, not the "
              "network, lost the message boundary.")
        return 1
    finally:
        sock.close()

    print(f"\nOK: all {a.n} replies parsed"
          + (" (read_frame respects the length prefix)" if a.fixed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
