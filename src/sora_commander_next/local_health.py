"""Bounded authenticated loopback health probe (never a remote MCP tool).

No proxy, DNS lookup, redirect, urllib environment handling or persistent secret.
Returns OK / DENY / FAIL only. Call from trusted local supervisor.
"""
import http.client
import re


class LocalHealthProbe:
    def __init__(self, port: int, token: bytes, *, timeout=0.35):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("invalid loopback port")
        if (type(token) is not bytes or re.fullmatch(rb"[0-9a-f]{64}", token) is None
                or len(set(token)) < 12):
            raise ValueError("invalid probe token")
        if type(timeout) not in (int, float) or not 0.05 <= timeout <= 1:
            raise ValueError("invalid probe timeout")
        self._port, self._token, self._timeout = port, token, timeout

    def __call__(self):
        connection = http.client.HTTPConnection("127.0.0.1", self._port,
                                                timeout=self._timeout)
        try:
            connection.request("GET", "/__sora_health",
                               headers={"Authorization": "Bearer " + self._token.decode("ascii"),
                                        "Cache-Control": "no-store"})
            response = connection.getresponse()
            response.read(0)
            if response.status == 204:
                return "OK"
            if response.status in (401, 403):
                return "DENY"
            return "FAIL"
        except (OSError, http.client.HTTPException, TimeoutError):
            return "FAIL"
        finally:
            connection.close()
