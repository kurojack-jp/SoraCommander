"""DEV_ONLY fixed Owner Broker startup/exit probe. HOLD_LIVE.

No commands, approval, transport, installation or protected-state access.
Importing this module never constructs or enters an Owner context.
"""
import argparse
from pathlib import Path
import sys
import time


_REQUEST_ID = "0123456789abcdef0123456789abcdef"


def _hold_ms(value):
    if (not 1 <= len(value) <= 4 or not value.isascii()
            or not value.isdecimal() or int(value) > 5000):
        raise argparse.ArgumentTypeError("hold-ms must be an integer in 0..5000")
    return int(value)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="DEV_ONLY Owner Broker lifecycle probe; HOLD_LIVE",
        add_help=False, allow_abbrev=False)
    parser.add_argument("--probe", action="store_true", required=True)
    parser.add_argument("--hold-ms", type=_hold_ms, default=0)
    args = parser.parse_args(argv)

    # Delayed until all arguments pass. The production constructor performs its
    # existing fixed-drive/non-reparse path checks; never resolve aliases here.
    from .broker_owner_process import BrokerOwnerProcess

    try:
        with BrokerOwnerProcess(sys.executable, str(Path(__file__).parent)) as broker:
            if broker.query_status_local(_REQUEST_ID) != {
                    "request_id": _REQUEST_ID, "status": "UNKNOWN"}:
                raise RuntimeError("unexpected probe state")
            print("OWNER_BROKER_PROBE_READY", flush=True)
            deadline = time.monotonic() + args.hold_ms / 1000
            while True:
                broker.query_status_local(_REQUEST_ID)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
        # Reached only after the production context confirms shutdown/release.
        print("OWNER_BROKER_PROBE_CLOSED", flush=True)
        return 0
    except Exception:
        # One attempt only. Never disclose exception details or claim cleanup.
        print("OWNER_BROKER_PROBE_DENIED", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
