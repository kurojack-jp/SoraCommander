"""Disposable loopback ASGI factory. No bind, credentials or startup on import."""

import hmac
import re
from contextlib import asynccontextmanager

from .host import Host
from .mcp_adapter import build_server


def create_loopback_app(token, *, host="127.0.0.1"):
    """Explicit test-only factory; caller must bind exclusively to 127.0.0.1.

    Supply a fresh secrets.token_hex(32), in memory only. Format/diversity checks
    reject obviously weak input; randomness cannot be established from a string.
    No caller-supplied Host/policy is accepted: this milestone always denies work.
    """
    if type(host) is not str or host != "127.0.0.1":
        raise ValueError("Only 127.0.0.1 is permitted")
    if (type(token) is not str or re.fullmatch(r"[0-9a-f]{64}", token) is None
            or len(set(token)) < 12
            or any(token == token[:n] * (64 // n) for n in (1, 2, 4, 8, 16, 32))):
        raise ValueError("A fresh random 256-bit hexadecimal test token is required")

    # Optional dependencies remain lazy so -S core discovery still works.
    from mcp.server.transport_security import TransportSecuritySettings

    owned_host = Host()
    server = build_server(owned_host)
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*"],
        allowed_origins=["http://127.0.0.1:*"],
    )
    server.settings.json_response = True
    server.settings.session_idle_timeout = 30
    server.settings.max_sessions = 16
    app = server.streamable_http_app()
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with sdk_lifespan(application):
                yield
        finally:
            owned_host.jobs.close()

    app.router.lifespan_context = lifespan
    return _BearerGate(app, token, health=owned_host.ping)


class _BearerGate:
    def __init__(self, app, token, health=None):
        self._app = app
        self._expected = b"Bearer " + token.encode("ascii")
        self._health = health

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [value for name, value in scope.get("headers", ())
                      if name.lower() == b"authorization"]
            # No header merging, whitespace normalization, query token or cookie
            # fallback. Gate all paths/methods before SDK routing/session handling.
            if len(values) != 1 or not hmac.compare_digest(values[0], self._expected):
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"www-authenticate", b"Bearer"), (b"cache-control", b"no-store"),
                    (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
            # Readiness is behind the same exact Bearer gate. Keep it local
            # and never echo command arguments, secrets, or exception messages.
            if scope.get("path") == "/__sora_health":
                headers = scope.get("headers", ())
                hosts = [v for k, v in headers if k.lower() == b"host"]
                origins = [v for k, v in headers if k.lower() == b"origin"]
                local_host = (len(hosts) == 1 and re.fullmatch(
                    rb"127\.0\.0\.1:[0-9]{1,5}", hosts[0]) is not None)
                ready = (self._health is not None and scope.get("method") == "GET"
                         and local_host and not origins)
                if ready:
                    try:
                        ready = self._health() == {
                            "status": "OK", "scaffold": True, "remote_ready": False}
                    except Exception:
                        ready = False
                await send({"type": "http.response.start", "status": 204 if ready else 503,
                            "headers": [(b"cache-control", b"no-store"),
                                        (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._app(scope, receive, send)
