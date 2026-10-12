from __future__ import annotations

import logging
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from uvicorn.logging import AccessFormatter
from websockets.sync.client import connect

from agent_core.log_redact import REDACTED, RedactTokenFilter, log_config, redact
from tests.conftest import pair_device

SECRET = "eyJkZXZpY2UiOiJhIn0=.0123abcd"


def record(name: str, msg: str, args: tuple) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, None)


def test_filter_redacts_http_access_record() -> None:
    rec = record(
        "uvicorn.access",
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", f"/sync/ws?token={SECRET}", "1.1", 403),
    )
    assert RedactTokenFilter().filter(rec) is True
    assert rec.args == ("127.0.0.1:5000", "GET", f"/sync/ws?token={REDACTED}", "1.1", 403)
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(rec)
    assert line == f'127.0.0.1:5000 - "GET /sync/ws?token={REDACTED} HTTP/1.1" 403 Forbidden'


def test_filter_redacts_websocket_accept_record() -> None:
    rec = record(
        "uvicorn.error",
        '%s - "WebSocket %s" [accepted]',
        ("127.0.0.1:5000", f"/sync/ws?token={SECRET}"),
    )
    RedactTokenFilter().filter(rec)
    assert rec.getMessage() == f'127.0.0.1:5000 - "WebSocket /sync/ws?token={REDACTED}" [accepted]'


def test_filter_redacts_preformatted_message() -> None:
    rec = record("uvicorn.error", f'127.0.0.1:5000 - "WebSocket /sync/ws?token={SECRET}" 403', ())
    RedactTokenFilter().filter(rec)
    assert rec.getMessage() == f'127.0.0.1:5000 - "WebSocket /sync/ws?token={REDACTED}" 403'


def test_filter_leaves_lines_without_token_untouched() -> None:
    args = ("127.0.0.1:5000", "GET", "/sync/pull?since=12&limit=50", "1.1", 200)
    rec = record("uvicorn.access", '%s - "%s %s HTTP/%s" %d', args)
    RedactTokenFilter().filter(rec)
    assert rec.msg == '%s - "%s %s HTTP/%s" %d'
    assert rec.args == args

    plain = record("uvicorn.error", "Uvicorn running on %s://%s:%d (Press CTRL+C to quit)", ("http", "0.0.0.0", 8787))
    RedactTokenFilter().filter(plain)
    assert plain.getMessage() == "Uvicorn running on http://0.0.0.0:8787 (Press CTRL+C to quit)"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (f"/sync/ws?v=2&token={SECRET}", f"/sync/ws?v=2&token={REDACTED}"),
        (f"/sync/ws?token={SECRET}&v=2", f"/sync/ws?token={REDACTED}&v=2"),
        (f"/sync/ws?a=1&token={SECRET}&v=2", f"/sync/ws?a=1&token={REDACTED}&v=2"),
        ("/sync/ws?token=abc%2Edef%26ghi&v=2", f"/sync/ws?token={REDACTED}&v=2"),
        ("/sync/ws%3Fa%3D1%26token%3Dabc.def%26v%3D2", f"/sync/ws%3Fa%3D1%26token%3D{REDACTED}%26v%3D2"),
        ("/sync/ws%253Ftoken%253Dabc.def%2526v%253D2", f"/sync/ws%253Ftoken%253D{REDACTED}%2526v%253D2"),
        (f"/sync/ws?TOKEN={SECRET}", f"/sync/ws?TOKEN={REDACTED}"),
        (f"/sync/ws?token={REDACTED}&v=2", f"/sync/ws?token={REDACTED}&v=2"),
    ],
)
def test_redact_query_positions_and_encodings(target: str, expected: str) -> None:
    assert redact(target) == expected


def test_server_log_has_path_but_not_token(hub: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    token = pair_device(hub, "code-alice", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa77")
    server = uvicorn.Server(
        uvicorn.Config(hub.app, host="127.0.0.1", port=0, log_level="info", log_config=log_config())
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        with connect(f"ws://127.0.0.1:{port}/sync/ws?v=2&token={token}") as ws:
            assert '"hello"' in ws.recv(timeout=5)
        with pytest.raises(Exception):
            connect(f"ws://127.0.0.1:{port}/sync/missing?token={token}")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            logging.getLogger(name).handlers.clear()

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert f'"WebSocket /sync/ws?v=2&token={REDACTED}" [accepted]' in output
    assert f"/sync/missing?token={REDACTED}" in output
    assert token not in output
    assert token.split(".")[0] not in output
