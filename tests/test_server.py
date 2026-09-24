"""Server behaviour: the three operations, error handling, and concurrency."""

import socket
import struct
import threading

import pytest

import protocol
from client import ServerError, VoteClient

POLL = "p-demo"


# ------------------------------------------------------------- happy paths
def test_register_issues_a_token(client):
    p = client.register("alice")
    assert p["voter_id"] == "alice"
    assert len(p["token"]) == 16
    assert p["already"] is False


def test_register_is_idempotent(client):
    first = client.register("alice")
    second = client.register("alice")
    assert second["token"] == first["token"]
    assert second["already"] is True


def test_vote_then_results(client):
    client.register("alice")
    ack = client.vote("alice", "B")
    assert ack["poll_id"] == POLL and ack["option_id"] == "B" and ack["seq"] == 1

    r = client.results()
    assert r["tallies"]["B"] == 1
    assert r["total"] == 1
    assert r["registered"] == 1


def test_tokens_survive_a_restart(server):
    """Tokens are HMAC-derived, not session state -- this is what lets the
    resilient client keep voting after the server comes back."""
    from server import PollStore
    token = PollStore().token_for("alice")
    assert token == server.store.token_for("alice")


# ------------------------------------------------------------ error paths
def test_duplicate_vote_is_rejected(client):
    client.register("alice")
    client.vote("alice", "B")
    with pytest.raises(ServerError) as e:
        client.vote("alice", "C")
    assert e.value.code == protocol.DUPLICATE_VOTE


def test_unknown_option(client):
    client.register("alice")
    with pytest.raises(ServerError) as e:
        client.vote("alice", "Z")
    assert e.value.code == protocol.UNKNOWN_OPTION


def test_unknown_poll(client):
    client.register("alice")
    client.poll_id = "p-nope"
    with pytest.raises(ServerError) as e:
        client.vote("alice", "B")
    assert e.value.code == protocol.UNKNOWN_POLL


def test_bad_token(client):
    with pytest.raises(ServerError) as e:
        client.vote("alice", "B", token="0" * 16)
    assert e.value.code == protocol.BAD_TOKEN


def test_missing_field(client):
    with pytest.raises(ServerError) as e:
        client.call("VOTE", {"voter_id": "alice"})
    assert e.value.code == protocol.MISSING_FIELD


def test_unknown_type(client):
    with pytest.raises(ServerError) as e:
        client.call("RECOUNT", {})
    assert e.value.code == protocol.UNKNOWN_TYPE


def test_semantic_error_keeps_the_connection_open(client):
    """The stream is still aligned, so the client may keep using it."""
    with pytest.raises(ServerError):
        client.call("RECOUNT", {})
    client.register("alice")
    assert client.vote("alice", "B")["seq"] == 1


def test_unsupported_version(server):
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    body = b'{"v":2,"t":"RESULTS","id":"x","p":{"poll_id":"p-demo"}}'
    sock.sendall(struct.pack("!I", len(body)) + body)
    msg = protocol.read_frame(sock)
    assert msg["t"] == "ERROR"
    assert msg["p"]["code"] == protocol.UNSUPPORTED_VERSION
    sock.close()


def test_malformed_frame_closes_the_connection(server):
    """An unparseable body means we can no longer find the next boundary."""
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    body = b"VOTE|alice|B"                      # not JSON
    sock.sendall(struct.pack("!I", len(body)) + body)
    msg = protocol.read_frame(sock)
    assert msg["t"] == "ERROR" and msg["p"]["code"] == protocol.BAD_JSON
    assert protocol.read_frame(sock) is None    # server hung up
    sock.close()


def test_reset_requires_the_admin_key(client):
    with pytest.raises(ServerError) as e:
        client.reset("wrong-key")
    assert e.value.code == protocol.NOT_AUTHORIZED


def test_reset_clears_the_ballot_box(client):
    client.register("alice")
    client.vote("alice", "B")
    client.reset()
    assert client.results()["total"] == 0
    assert client.vote("alice", "C")["seq"] == 1   # alice may vote again


# ------------------------------------------------------------- concurrency
def test_five_concurrent_clients_lose_no_votes(server):
    clients, ops = 5, 20

    def run(idx):
        with VoteClient("127.0.0.1", server.port, timeout=10) as c:
            for j in range(ops):
                c.vote(f"c{idx}-{j:03d}", "ABCD"[j % 4])

    threads = [threading.Thread(target=run, args=(i,)) for i in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with VoteClient("127.0.0.1", server.port, timeout=10) as c:
        r = c.results()
    assert r["total"] == clients * ops
    assert sum(r["tallies"].values()) == clients * ops
    assert server.store.seq == clients * ops


def test_concurrent_duplicate_votes_count_once(server):
    """Two threads racing on the same voter: exactly one ballot is recorded."""
    outcomes = []

    def run(option):
        with VoteClient("127.0.0.1", server.port, timeout=10) as c:
            try:
                c.vote("alice", option)
                outcomes.append("ok")
            except ServerError as e:
                outcomes.append(e.code)

    threads = [threading.Thread(target=run, args=(o,)) for o in ("B", "C")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count(protocol.DUPLICATE_VOTE) == 1
    with VoteClient("127.0.0.1", server.port, timeout=10) as c:
        assert c.results()["total"] == 1
