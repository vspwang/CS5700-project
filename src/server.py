"""Polly -- TCP voting/polling server speaking VPP/1.

Concurrency: one OS thread per accepted connection (thread-per-connection).
The accept loop runs on the main thread and hands each connection to a daemon
thread; all shared state lives in PollStore behind a single mutex.  Blocking
socket calls in a worker thread therefore never block another client.

Usage:
    python src/server.py --port 5700
"""

import argparse
import hmac
import logging
import socket
import threading

import protocol
from protocol import ProtocolError

LOG = logging.getLogger("polly.server")

DEFAULT_POLL = "p-demo"
DEFAULT_TITLE = "Which transport protocol should we study next?"
DEFAULT_OPTIONS = ["A", "B", "C", "D"]
DEFAULT_SECRET = b"cs5700-polly-dev-secret"
DEFAULT_ADMIN_KEY = "cs5700-admin"


class PollStore:
    """All mutable poll state, guarded by one lock.

    Voter tokens are derived with HMAC rather than stored in a session table,
    so a token stays valid across a server restart.  That is what lets the
    resilient client (Part 7) reconnect and keep voting without re-registering.
    """

    def __init__(self, poll_id=DEFAULT_POLL, title=DEFAULT_TITLE,
                 options=None, secret=DEFAULT_SECRET,
                 admin_key=DEFAULT_ADMIN_KEY):
        self.poll_id = poll_id
        self.title = title
        self.options = list(options or DEFAULT_OPTIONS)
        self._secret = secret
        self._admin_key = admin_key
        self._lock = threading.Lock()
        self.registered = set()
        self.votes = {}                                  # voter_id -> option_id
        self.tallies = {o: 0 for o in self.options}
        self.seq = 0

    def token_for(self, voter_id):
        return hmac.new(self._secret, voter_id.encode(), "sha256").hexdigest()[:16]

    def _check_poll(self, poll_id):
        if poll_id != self.poll_id:
            raise ProtocolError(protocol.UNKNOWN_POLL, f"no such poll {poll_id!r}")

    # -- operations ---------------------------------------------------------
    def register(self, voter_id):
        """Idempotent: re-registering returns the same token."""
        with self._lock:
            already = voter_id in self.registered
            self.registered.add(voter_id)
        return self.token_for(voter_id), already

    def vote(self, voter_id, poll_id, option_id, token):
        self._check_poll(poll_id)
        if not hmac.compare_digest(token, self.token_for(voter_id)):
            raise ProtocolError(protocol.BAD_TOKEN, "token does not match voter_id")
        if option_id not in self.tallies:
            raise ProtocolError(protocol.UNKNOWN_OPTION, f"no such option {option_id!r}")
        with self._lock:
            if voter_id in self.votes:
                raise ProtocolError(protocol.DUPLICATE_VOTE,
                                    f"{voter_id} already voted in {poll_id}")
            self.seq += 1
            self.votes[voter_id] = option_id
            self.tallies[option_id] += 1
            return self.seq

    def results(self, poll_id):
        self._check_poll(poll_id)
        with self._lock:
            return {
                "poll_id": self.poll_id,
                "title": self.title,
                "options": self.options,
                "tallies": dict(self.tallies),
                "total": sum(self.tallies.values()),
                "registered": len(self.registered),
            }

    def reset(self, admin_key):
        """Clear the ballot box so an experiment can be re-run from scratch."""
        if not hmac.compare_digest(admin_key, self._admin_key):
            raise ProtocolError(protocol.NOT_AUTHORIZED, "bad admin key")
        with self._lock:
            self.registered.clear()
            self.votes.clear()
            self.tallies = {o: 0 for o in self.options}
            self.seq = 0


def dispatch(store, msg):
    """Map one request message onto (response_type, response_payload)."""
    if msg.get("v") != protocol.VERSION:
        raise ProtocolError(protocol.UNSUPPORTED_VERSION, f"expected v={protocol.VERSION}")
    mtype = msg.get("t")
    p = msg.get("p") or {}
    if not isinstance(p, dict):
        raise ProtocolError(protocol.MISSING_FIELD, "payload must be an object")

    if mtype == "REGISTER":
        voter_id = protocol.field(p, "voter_id")
        token, already = store.register(voter_id)
        return "REGISTER_OK", {"voter_id": voter_id, "token": token, "already": already}

    if mtype == "VOTE":
        voter_id = protocol.field(p, "voter_id")
        poll_id = protocol.field(p, "poll_id")
        option_id = protocol.field(p, "option_id")
        token = protocol.field(p, "token")
        seq = store.vote(voter_id, poll_id, option_id, token)
        return "VOTE_ACK", {"poll_id": poll_id, "option_id": option_id, "seq": seq}

    if mtype == "RESULTS":
        return "RESULTS_OK", store.results(protocol.field(p, "poll_id"))

    if mtype == "RESET":
        store.reset(protocol.field(p, "admin_key"))
        return "RESET_OK", {"poll_id": store.poll_id}

    raise ProtocolError(protocol.UNKNOWN_TYPE, f"unknown message type {mtype!r}")


class VoteServer:
    def __init__(self, host="127.0.0.1", port=5700, store=None):
        self.host = host
        self.port = port
        self.store = store or PollStore()
        self.sock = None
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        """Bind and listen.  Returns the port actually bound (useful with 0)."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Without SO_REUSEADDR, restarting the server while old connections sit
        # in TIME_WAIT fails with EADDRINUSE -- see Experiment 4.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        LOG.info("listening on %s:%d poll=%s options=%s",
                 self.host, self.port, self.store.poll_id, self.store.options)
        return self.port

    def serve_forever(self):
        while not self._stop.is_set():
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(conn, addr), daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self):
        self._stop.set()
        if self.sock:
            self.sock.close()

    # -- one connection -----------------------------------------------------
    def _handle(self, conn, addr):
        LOG.info("connect %s", addr)
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while True:
                try:
                    msg = protocol.read_frame(conn)
                except ProtocolError as e:
                    # The stream is out of sync; answer once, then close.
                    LOG.warning("%s framing error: %s", addr, e)
                    self._send_error(conn, None, e)
                    return
                except (ConnectionError, OSError) as e:
                    LOG.info("%s read failed: %s", addr, e)
                    return
                if msg is None:
                    LOG.info("disconnect %s (clean EOF)", addr)
                    return

                req_id = msg.get("id")
                try:
                    rtype, payload = dispatch(self.store, msg)
                except ProtocolError as e:
                    self._send_error(conn, req_id, e)
                    if not e.recoverable:
                        return
                    continue
                except Exception:                      # pragma: no cover
                    LOG.exception("handler crashed")
                    self._send_error(conn, req_id,
                                     ProtocolError(protocol.INTERNAL, "server error"))
                    continue
                protocol.send(conn, rtype, req_id, payload)
        except (ConnectionError, OSError) as e:
            LOG.info("%s connection lost: %s", addr, e)
        finally:
            conn.close()

    @staticmethod
    def _send_error(conn, req_id, err):
        try:
            protocol.send(conn, "ERROR", req_id, {"code": err.code, "msg": err.msg})
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Polly VPP/1 voting server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5700)
    ap.add_argument("--poll-id", default=DEFAULT_POLL)
    ap.add_argument("--options", default=",".join(DEFAULT_OPTIONS),
                    help="comma-separated option ids")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(message)s")
    store = PollStore(poll_id=args.poll_id, options=args.options.split(","))
    server = VoteServer(args.host, args.port, store)
    server.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
