import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from server import PollStore, VoteServer  # noqa: E402
from client import VoteClient  # noqa: E402


@pytest.fixture
def server():
    """A VoteServer on an ephemeral port, serving in a background thread."""
    srv = VoteServer("127.0.0.1", 0, PollStore())
    srv.start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def client(server):
    c = VoteClient("127.0.0.1", server.port, timeout=5)
    c.connect()
    yield c
    c.close()
