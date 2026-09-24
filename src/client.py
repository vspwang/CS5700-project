"""Polly client library + CLI (VPP/1).

Two behaviours, selected by one flag, so the Part 7 before/after comparison
changes exactly one variable:

  resilient=False  (BEFORE) -- no socket timeout, no retry.  A dead or frozen
                               server surfaces as an exception and the run ends.
  resilient=True   (AFTER)  -- socket timeout + bounded exponential backoff
                               reconnect, replaying the failed request with the
                               same request id.

Usage:
    python src/client.py register --voter alice
    python src/client.py vote     --voter alice --option B
    python src/client.py results
    python src/client.py reset
"""

import argparse
import itertools
import json
import random
import socket
import time

import protocol
from protocol import ProtocolError

DEFAULT_POLL = "p-demo"
DEFAULT_ADMIN_KEY = "cs5700-admin"


class ServerError(Exception):
    """The server answered with an ERROR message."""

    def __init__(self, code, msg=""):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


class VoteClient:
    def __init__(self, host="127.0.0.1", port=5700, poll_id=DEFAULT_POLL,
                 timeout=None, resilient=False, retries=5, backoff=0.1,
                 backoff_cap=1.6):
        self.host = host
        self.port = port
        self.poll_id = poll_id
        self.timeout = timeout
        self.resilient = resilient
        self.retries = retries
        self.backoff = backoff
        self.backoff_cap = backoff_cap
        self.sock = None
        self.tokens = {}
        self.reconnects = 0
        self._ids = itertools.count(1)
        self._tag = f"c{random.randrange(1 << 20):05x}"

    # -- connection ---------------------------------------------------------
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.timeout is not None:
            self.sock.settimeout(self.timeout)
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _drop(self):
        """Discard a socket we no longer trust, so the next call reconnects."""
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    # -- request/response ---------------------------------------------------
    def _call_once(self, mtype, payload, req_id):
        if self.sock is None:
            self.connect()
        protocol.send(self.sock, mtype, req_id, payload)
        msg = protocol.read_frame(self.sock)
        if msg is None:
            raise ConnectionError("server closed the connection")
        if msg.get("id") != req_id:
            raise ProtocolError(protocol.BAD_FRAME,
                                f"response id {msg.get('id')!r} != request id {req_id!r}")
        if msg.get("t") == "ERROR":
            p = msg.get("p") or {}
            raise ServerError(p.get("code", "?"), p.get("msg", ""))
        return msg

    def call(self, mtype, payload=None):
        req_id = f"{self._tag}-{next(self._ids):06d}"
        payload = payload or {}
        if not self.resilient:
            return self._call_once(mtype, payload, req_id)

        delay = self.backoff
        for attempt in range(self.retries + 1):
            try:
                return self._call_once(mtype, payload, req_id)
            except ServerError as e:
                # A vote that we retried may have been counted before the
                # failure; one-vote-per-voter means DUPLICATE_VOTE on a retry
                # is proof the original request landed, i.e. success.
                if attempt and mtype == "VOTE" and e.code == protocol.DUPLICATE_VOTE:
                    return {"v": protocol.VERSION, "t": "VOTE_ACK", "id": req_id,
                            "p": {"poll_id": payload.get("poll_id"),
                                  "option_id": payload.get("option_id"),
                                  "seq": None, "recovered": True}}
                raise
            except (OSError, ProtocolError):
                # OSError covers socket.timeout, ConnectionReset, BrokenPipe.
                self._drop()
                if attempt == self.retries:
                    raise
                time.sleep(delay + random.uniform(0, delay * 0.1))
                delay = min(delay * 2, self.backoff_cap)
                self.reconnects += 1

    # -- the three application operations -----------------------------------
    def register(self, voter_id):
        p = self.call("REGISTER", {"voter_id": voter_id})["p"]
        self.tokens[voter_id] = p["token"]
        return p

    def vote(self, voter_id, option_id, token=None):
        token = token or self.tokens.get(voter_id) or self.register(voter_id)["token"]
        return self.call("VOTE", {"voter_id": voter_id, "poll_id": self.poll_id,
                                  "option_id": option_id, "token": token})["p"]

    def results(self):
        return self.call("RESULTS", {"poll_id": self.poll_id})["p"]

    def reset(self, admin_key=DEFAULT_ADMIN_KEY):
        return self.call("RESET", {"admin_key": admin_key})["p"]


def main():
    ap = argparse.ArgumentParser(description="Polly VPP/1 voting client")
    ap.add_argument("command", choices=["register", "vote", "results", "reset"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5700)
    ap.add_argument("--poll-id", default=DEFAULT_POLL)
    ap.add_argument("--voter", default="alice")
    ap.add_argument("--option", default="B")
    ap.add_argument("--admin-key", default=DEFAULT_ADMIN_KEY)
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--resilient", action="store_true")
    args = ap.parse_args()

    client = VoteClient(args.host, args.port, args.poll_id,
                        timeout=args.timeout, resilient=args.resilient)
    try:
        with client:
            if args.command == "register":
                out = client.register(args.voter)
            elif args.command == "vote":
                out = client.vote(args.voter, args.option)
            elif args.command == "results":
                out = client.results()
            else:
                out = client.reset(args.admin_key)
        print(json.dumps(out, indent=2))
    except ServerError as e:
        print(f"server error: {e}")
        raise SystemExit(1)
    except (OSError, ProtocolError) as e:
        print(f"transport error: {e}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
