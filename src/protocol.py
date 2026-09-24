"""VPP/1 -- Voting & Polling Protocol, version 1.

Framing
-------
Every application message is carried in exactly one frame:

    +--------+--------+--------+--------+--------- ... ---------+
    |      length N (uint32, big endian)  |   N bytes UTF-8 JSON  |
    +--------+--------+--------+--------+--------- ... ---------+

TCP delivers a byte stream, not messages: a single recv() may return half a
frame, one frame, or three frames.  A conforming receiver must read exactly 4
header bytes, decode N, then read exactly N body bytes, looping until it has
them.  recv_exact()/read_frame() below are the only sanctioned way to read
VPP/1 off a socket; mystery/buggy_client.py shows what happens when you skip
them.

Envelope
--------
    {"v": 1, "t": "<TYPE>", "id": "<request id>", "p": {<payload>}}

  v   protocol version, always 1
  t   message type
  id  client-generated request id, echoed verbatim in the response so a client
      can match a reply to its request
  p   type-specific payload object
"""

import json
import struct

VERSION = 1
HEADER = struct.Struct("!I")  # 4-byte big-endian length prefix
MAX_FRAME = 65536             # bytes; anything larger is rejected

# ---------------------------------------------------------------- error codes
BAD_FRAME = "BAD_FRAME"                      # length or bytes unusable
BAD_JSON = "BAD_JSON"                        # body is not a JSON object
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
MSG_TOO_LARGE = "MSG_TOO_LARGE"
UNKNOWN_TYPE = "UNKNOWN_TYPE"
MISSING_FIELD = "MISSING_FIELD"
UNKNOWN_POLL = "UNKNOWN_POLL"
UNKNOWN_OPTION = "UNKNOWN_OPTION"
BAD_TOKEN = "BAD_TOKEN"
DUPLICATE_VOTE = "DUPLICATE_VOTE"
NOT_AUTHORIZED = "NOT_AUTHORIZED"
INTERNAL = "INTERNAL"

# Codes that leave the byte stream in a known-good position.  Anything else
# means we can no longer find the next frame boundary, so the server replies
# once and then closes the connection.
RECOVERABLE = {
    UNSUPPORTED_VERSION, UNKNOWN_TYPE, MISSING_FIELD, UNKNOWN_POLL,
    UNKNOWN_OPTION, BAD_TOKEN, DUPLICATE_VOTE, NOT_AUTHORIZED, INTERNAL,
}


class ProtocolError(Exception):
    """An application-level failure that maps onto an ERROR response."""

    def __init__(self, code, msg=""):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg

    @property
    def recoverable(self):
        return self.code in RECOVERABLE


# ------------------------------------------------------------------- encoding
def encode(mtype, req_id, payload=None):
    """Serialize one message into a complete frame (header + body)."""
    body = json.dumps(
        {"v": VERSION, "t": mtype, "id": req_id, "p": payload or {}},
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError(MSG_TOO_LARGE, f"{len(body)} > {MAX_FRAME}")
    return HEADER.pack(len(body)) + body


def send(sock, mtype, req_id, payload=None):
    """Write one frame with a single sendall() so it is never interleaved."""
    sock.sendall(encode(mtype, req_id, payload))


# ------------------------------------------------------------------- decoding
def recv_exact(sock, n, allow_eof=False):
    """Read exactly n bytes, looping because one recv() != one message.

    Returns None only when allow_eof is set and the peer closed cleanly on a
    frame boundary (i.e. zero bytes read).  A close in the middle of a frame is
    a truncated message and raises.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if allow_eof and not buf:
                return None
            raise ConnectionError(f"peer closed mid-frame ({len(buf)}/{n} bytes)")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock):
    """Read one complete application message.  Returns None on a clean EOF."""
    head = recv_exact(sock, HEADER.size, allow_eof=True)
    if head is None:
        return None
    (n,) = HEADER.unpack(head)
    if n == 0:
        raise ProtocolError(BAD_FRAME, "zero-length frame")
    if n > MAX_FRAME:
        # We cannot trust the length, so we cannot find the next boundary.
        raise ProtocolError(MSG_TOO_LARGE, f"declared {n} > {MAX_FRAME}")

    body = recv_exact(sock, n)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError(BAD_FRAME, "body is not valid UTF-8")
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(BAD_JSON, str(e))
    if not isinstance(msg, dict):
        raise ProtocolError(BAD_JSON, "body is not a JSON object")
    return msg


# --------------------------------------------------------------- field access
def field(payload, name):
    """Fetch a required non-empty string field, or raise MISSING_FIELD."""
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ProtocolError(MISSING_FIELD, f"missing or invalid field {name!r}")
    return value
