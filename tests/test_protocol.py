"""Framing tests: one recv() is not one application message."""

import json
import struct

import pytest

import protocol
from protocol import ProtocolError


class FakeSock:
    """A socket whose recv() hands back at most `chunk` bytes at a time."""

    def __init__(self, data, chunk=4096):
        self.data = bytes(data)
        self.pos = 0
        self.chunk = chunk

    def recv(self, n):
        n = min(n, self.chunk, len(self.data) - self.pos)
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out


def frame(mtype="VOTE", req_id="r1", payload=None):
    return protocol.encode(mtype, req_id, payload or {"option_id": "B"})


def test_roundtrip():
    msg = protocol.read_frame(FakeSock(frame()))
    assert msg["v"] == protocol.VERSION
    assert msg["t"] == "VOTE"
    assert msg["id"] == "r1"
    assert msg["p"]["option_id"] == "B"


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 4096])
def test_split_across_recvs(chunk):
    """A frame delivered one byte at a time must still parse."""
    assert protocol.read_frame(FakeSock(frame(), chunk=chunk))["t"] == "VOTE"


def test_three_frames_in_one_recv():
    """Coalesced frames must be returned as three separate messages."""
    blob = b"".join(frame(req_id=f"r{i}") for i in range(3))
    sock = FakeSock(blob)
    assert [protocol.read_frame(sock)["id"] for _ in range(3)] == ["r0", "r1", "r2"]
    assert protocol.read_frame(sock) is None


def test_clean_eof_returns_none():
    assert protocol.read_frame(FakeSock(b"")) is None


def test_truncated_header_raises():
    with pytest.raises(ConnectionError):
        protocol.read_frame(FakeSock(frame()[:2]))


def test_truncated_body_raises():
    with pytest.raises(ConnectionError):
        protocol.read_frame(FakeSock(frame()[:-5]))


def test_zero_length_frame():
    with pytest.raises(ProtocolError) as e:
        protocol.read_frame(FakeSock(struct.pack("!I", 0)))
    assert e.value.code == protocol.BAD_FRAME


def test_oversized_length_prefix():
    with pytest.raises(ProtocolError) as e:
        protocol.read_frame(FakeSock(struct.pack("!I", protocol.MAX_FRAME + 1)))
    assert e.value.code == protocol.MSG_TOO_LARGE


def test_body_not_utf8():
    body = b"\xff\xfe\xfd"
    with pytest.raises(ProtocolError) as e:
        protocol.read_frame(FakeSock(struct.pack("!I", len(body)) + body))
    assert e.value.code == protocol.BAD_FRAME


def test_body_not_json():
    body = b"LOGIN|maryam"
    with pytest.raises(ProtocolError) as e:
        protocol.read_frame(FakeSock(struct.pack("!I", len(body)) + body))
    assert e.value.code == protocol.BAD_JSON


def test_body_json_but_not_an_object():
    body = json.dumps([1, 2, 3]).encode()
    with pytest.raises(ProtocolError) as e:
        protocol.read_frame(FakeSock(struct.pack("!I", len(body)) + body))
    assert e.value.code == protocol.BAD_JSON


def test_encode_rejects_oversized_message():
    with pytest.raises(ProtocolError) as e:
        protocol.encode("VOTE", "r1", {"blob": "x" * (protocol.MAX_FRAME + 1)})
    assert e.value.code == protocol.MSG_TOO_LARGE


def test_field_helper():
    assert protocol.field({"a": "x"}, "a") == "x"
    for bad in ({}, {"a": ""}, {"a": 7}):
        with pytest.raises(ProtocolError) as e:
            protocol.field(bad, "a")
        assert e.value.code == protocol.MISSING_FIELD
