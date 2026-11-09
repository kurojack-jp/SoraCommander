"""Explicit Phase 7 disposable loopback MCP host.

Usage (local tests only): Python phase7_http_host.py <port>
The 256-bit temporary token is provided through anonymous stdin. No token in
argv, environment, output, URLs, or files. No mutable command policy/grants.
"""
import os
from pathlib import Path
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main():
    if (len(sys.argv) not in (2, 3) or not sys.argv[1].isascii()
            or not sys.argv[1].isdigit()):
        raise ValueError("explicit loopback test port required")
    # Optional fixture-only deliberate event-loop stall. NEVER an MCP tool.
    marker = Path(sys.argv[2]) if len(sys.argv) == 3 else None
    if marker is not None and not marker.is_absolute():
        raise ValueError("absolute fixture marker required")
    port = int(sys.argv[1])
    if not 1024 <= port <= 65535:
        raise ValueError("invalid loopback test port")

    # No unattended orphan: running fixture exits if its supervisor disappears.
    timer = threading.Timer(35, lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    try:
        raw = sys.stdin.buffer.readline(128)
        if not raw.endswith(b"\n"):
            raise ValueError("missing anonymous-pipe startup token")
        token = raw[:-1].decode("ascii")
        import uvicorn
        from sora_commander_next.http_local import create_loopback_app
        app = create_loopback_app(token, host="127.0.0.1")
        if marker is not None:
            inner_app = app

            async def fixture_stall(scope, receive, send):
                if (scope["type"] == "http"
                        and scope.get("path") == "/__sora_health"
                        and marker.exists()):
                    time.sleep(12)  # intentionally block disposable ASGI loop
                await inner_app(scope, receive, send)

            app = fixture_stall
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
            listener.listen(32)
            config = uvicorn.Config(app, host="127.0.0.1", port=0,
                access_log=False, log_level="critical", proxy_headers=False,
                lifespan="on", timeout_graceful_shutdown=2, ws="none")
            server = uvicorn.Server(config)

            def stop_if_supervisor_gone():
                sys.stdin.buffer.read()
                server.should_exit = True

            threading.Thread(target=stop_if_supervisor_gone, daemon=True).start()
            server.run(sockets=[listener])
    finally:
        timer.cancel()


if __name__ == "__main__":
    main()
