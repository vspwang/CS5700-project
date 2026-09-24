"""Part 7: the resilient client recovers from a server that is not there yet.

Full outage-and-restart behaviour is measured by experiments/exp_failure.py;
these tests pin the mechanism so a refactor cannot silently remove it.
"""

import socket
import threading

import pytest

from client import VoteClient
from server import PollStore, VoteServer


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_later(port, delay):
    """Bring a server up after `delay` seconds; returns it once started."""
    holder = {}
    ready = threading.Event()

    def boot():
        srv = VoteServer("127.0.0.1", port, PollStore())
        srv.start()
        holder["srv"] = srv
        ready.set()
        srv.serve_forever()

    timer = threading.Timer(delay, lambda: threading.Thread(target=boot, daemon=True).start())
    timer.start()
    return holder, ready, timer


def test_baseline_client_fails_immediately():
    c = VoteClient("127.0.0.1", free_port(), timeout=2, resilient=False)
    with pytest.raises(OSError):
        c.register("alice")


def test_resilient_client_retries_until_the_server_is_back():
    port = free_port()
    holder, ready, timer = start_later(port, 0.4)
    try:
        c = VoteClient("127.0.0.1", port, timeout=2, resilient=True,
                       retries=8, backoff=0.05)
        assert c.vote("alice", "B")["option_id"] == "B"
        assert c.reconnects > 0
        c.close()
    finally:
        timer.join()
        ready.wait(5)
        if "srv" in holder:
            holder["srv"].shutdown()


def test_retry_reuses_the_request_id():
    """A replayed VOTE must not be able to count as a second ballot."""
    seen = []
    c = VoteClient("127.0.0.1", free_port(), timeout=0.5, resilient=True,
                   retries=2, backoff=0.01)
    original = c._call_once

    def spy(mtype, payload, req_id):
        seen.append(req_id)
        return original(mtype, payload, req_id)

    c._call_once = spy
    with pytest.raises(OSError):
        c.vote("alice", "B", token="0" * 16)   # token supplied: no REGISTER first
    assert len(seen) == 3            # one attempt + two retries
    assert len(set(seen)) == 1       # all carrying the same request id
