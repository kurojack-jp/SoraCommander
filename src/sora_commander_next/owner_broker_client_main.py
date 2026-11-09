"""DEV_ONLY MCP Host that forwards to a running Owner Broker. It never starts one.

The tunnel starts this in place of the usual Host. If no Broker answers, reads
still work, and commands and writes return UNAVAILABLE.
"""
import os
from pathlib import Path

from .mcp_adapter import serve_stdio
from .owner_broker_dev import DevBrokerHost
from .owner_broker_pipe import OwnerBrokerPipeClient, broker_pipe_name
from .allowed_roots import AllowedRootsError, startup_allowed_roots
from .windows_owner import OwnerSessionGuard


def build_client_host(guard, repo_root, environ, *, pipe_name=None):
    roots = startup_allowed_roots(
        repo_root, environ.get("SORA_NEXT_OWNER_PROFILE", "tests"), environ)
    name = pipe_name or broker_pipe_name(guard.sid, guard._identity.session_id)
    return DevBrokerHost(OwnerBrokerPipeClient(pipe_name=name), read_roots=roots.full,
                         read_only_roots=roots.read_only,
                         edit_pilot=environ.get("SORA_NEXT_EDIT_PILOT") == "1",
                         create_pilot=environ.get("SORA_NEXT_CREATE_PILOT") == "1")


def main():
    guard = OwnerSessionGuard()
    guard.verify()
    try:
        host = build_client_host(guard, Path(__file__).resolve().parents[2], os.environ)
    except AllowedRootsError as error:
        import sys
        print(f"[HOLD] {error}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    serve_stdio(host)


if __name__ == "__main__":
    main()
