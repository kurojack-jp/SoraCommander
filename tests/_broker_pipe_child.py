"""Test child: serves a fake backend over the real pipe. Never starts a job or window."""
import sys
import time

from sora_commander_next.owner_broker_pipe import OwnerBrokerPipeServer
from sora_commander_next.owner_broker_service import OwnerBrokerService, _SCHEMAS
from sora_commander_next.windows_owner import OwnerSessionGuard


class Backend:
    """Records nothing outside this process and has no launch path."""

    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        if name not in _SCHEMAS:
            raise AttributeError(name)

        def call(**arguments):
            self.calls += 1
            if name == "request_status":
                return {"status": "OK", "request_id": arguments["request_id"],
                        "calls": self.calls}
            if name == "await_process_output":
                time.sleep(arguments.get("wait_seconds", 0))
                return {"status": "OK", "waited": True}
            if name == "read_process_output":
                return {"status": "OK", "stdout": "x" * 200000}
            if name == "stop_process":
                # The call never returns, so the client sees the pipe break.
                import os
                os._exit(9)
            return {"status": "OK", "sessions": [], "count": 0}
        return call


def main():
    server = OwnerBrokerPipeServer(owner_guard=OwnerSessionGuard(),
                                   service=OwnerBrokerService(Backend()),
                                   pipe_name=sys.argv[1], workers=3)
    server.start()
    print("READY", flush=True)
    sys.stdin.readline()
    print("STOPPED" if server.stop() else "STILL_RUNNING", flush=True)


if __name__ == "__main__":
    main()
