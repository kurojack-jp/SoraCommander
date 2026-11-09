"""DEV-only Owner identity-bound independent process probe.

This is NOT a Broker, service, approval endpoint, or remotely callable process.
It starts NO commands, accepts NO authorization claims, has NO network/pipe,
and never touches protected data. Unprotected DEV source cannot establish
production code provenance. Only short, explicit Owner-local observations.
"""
import argparse
import math
import time

from .offline_broker_singleton import DevLeaseError, DevWindowsMutexLease
from .windows_owner import OwnerSessionGuard


def main(argv=None):
    parser = argparse.ArgumentParser(description="DEV-only Owner process probe")
    parser.add_argument("--name", required=True)
    parser.add_argument("--hold-seconds", type=float, default=1.5)
    args = parser.parse_args(argv)
    if not math.isfinite(args.hold_seconds) or not 0.5 <= args.hold_seconds <= 4:
        parser.error("hold seconds must be finite and in [0.5, 4]")
    # Verify the actual process token, login session and Windows unlock state.
    # There is no --sid, --luid, --session, --owner or guard override.
    try:
        guard = OwnerSessionGuard()
        guard.verify()
        with DevWindowsMutexLease(args.name):
            guard.verify()
            print("DEV_OWNER_VERIFIED", flush=True)
            deadline = time.monotonic() + args.hold_seconds
            while True:
                guard.verify()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.1, remaining))
            guard.verify()
    except (PermissionError, OSError):
        # No Owner identifying values or command context are sent to stdout.
        print("DEV_OWNER_UNAVAILABLE", flush=True)
        return 4
    except DevLeaseError:
        print("DEV_LEASE_HELD_OR_UNKNOWN", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
