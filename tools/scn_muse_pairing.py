"""In-memory one-time pairing for the Muse exec bridge."""

from datetime import UTC, datetime, timedelta
import hmac
import json
import secrets
import threading
import time
from typing import Any


_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_TTL = 600
_BINDING_TTL = 28800


class Pairing:
    def __init__(self, clock=time.monotonic, wall=lambda: datetime.now(UTC), rng=secrets):
        self._clock, self._wall = clock, wall
        self._code_deadline = clock() + _CODE_TTL
        self._code = "".join(rng.choice(_ALPHABET) for _ in range(8))
        self._lock = threading.Lock()
        self._closed_status = None
        self._failures = 0
        self._bound_id = None
        self._binding_deadline = 0

    def display(self) -> str:
        return f"{self._code[:4]}-{self._code[4:]}"

    def pair(self, code, stable_id: str) -> dict[str, Any]:
        with self._lock:
            if self._closed_status is not None:
                return {"status": self._closed_status}

            now = self._clock()
            suffix = stable_id[-6:]
            if now >= self._code_deadline:
                self._closed_status = "PAIRING_EXPIRED"
                print(f"[bridge] pairing code expired (peer …{suffix})")
                return {"status": self._closed_status}

            candidate = code.strip().replace("-", "").upper() if isinstance(code, str) and len(code) <= 32 else ""
            if not candidate.isascii() or not hmac.compare_digest(candidate, self._code):
                self._failures += 1
                print(f"[bridge] pairing failed (peer …{suffix})")
                if self._failures == 5:
                    self._closed_status = "PAIRING_LOCKED"
                    print(f"[bridge] pairing locked (peer …{suffix})")
                return {"status": "PAIRING_FAILED"}

            until = self._wall().astimezone(UTC) + timedelta(seconds=_BINDING_TTL)
            self._bound_id = stable_id
            self._binding_deadline = now + _BINDING_TTL
            self._closed_status = "PAIRING_LOCKED"
            print(f"[bridge] pairing succeeded (peer …{suffix})")
            return {"status": "PAIRED", "until": until.isoformat().replace("+00:00", "Z")}

    def allows(self, stable_id: str | None) -> bool:
        with self._lock:
            if self._bound_id is not None and self._clock() >= self._binding_deadline:
                print(f"[bridge] binding expired (peer …{self._bound_id[-6:]})")
                self._bound_id = None
            return self._bound_id is not None and stable_id == self._bound_id


def register_pairing(server, pairing):
    """Gate the single SDK tool-call entry point before it can dispatch any tool."""
    from mcp.types import CallToolResult, TextContent

    def caller():
        try:
            request = server.get_context().request_context.request
        except ValueError:
            return None
        state = getattr(request, "scope", {}).get("state", {})
        stable_id = state.get("muse_stable_id")
        return stable_id if isinstance(stable_id, str) and stable_id else None

    @server.tool()
    def exec_pair(code: str) -> dict[str, Any]:
        """Pair this Tailscale node with the one-time code from the Owner.

        PAIRED includes the UTC expiry. Owner review still applies to commands.
        On PAIRING_FAILED, PAIRING_LOCKED, PAIRING_EXPIRED or PAIRING_REQUIRED,
        report the status and wait for the Owner. Never guess or log the code.
        """
        stable_id = caller()
        if stable_id is None:
            return {"status": "PAIRING_REQUIRED"}
        return pairing.pair(code, stable_id)

    original_call_tool = server.call_tool

    async def guarded_call_tool(name, arguments):
        if name != "exec_pair" and not pairing.allows(caller()):
            refusal = {"status": "PAIRING_REQUIRED"}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(refusal))],
                                  structuredContent=refusal)
        return await original_call_tool(name, arguments)

    # FastMCP captures the original bound method during construction. Replace
    # both the HTTP handler and the entry point used for direct SDK calls.
    server._mcp_server.call_tool(validate_input=False)(guarded_call_tool)
    server.call_tool = guarded_call_tool
