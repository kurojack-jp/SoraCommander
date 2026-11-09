"""DEV-only independent-process lifetime probe; no Broker authority or IPC.

This intentionally inert process owns only a short-lived Windows DEV mutex.
It NEVER imports the production Host, starts jobs, reads Owner data, or listens
on a network/pipe. stdout communicates two fixed observation markers only.
"""
import argparse
import math
import time

from .offline_broker_singleton import DevLeaseError, DevWindowsMutexLease


def main(argv=None):
    parser = argparse.ArgumentParser(description="DEV-only inert process lifetime probe")
    parser.add_argument("--name", required=True)
    parser.add_argument("--hold-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.hold_seconds) or not 0.5 <= args.hold_seconds <= 5:
        parser.error("hold seconds must be finite and in [0.5, 5]")
    try:
        lease = DevWindowsMutexLease(args.name)
        with lease:
            print("PROBE_READY", flush=True)
            # This process does not consume stdin or depend on the observer
            # staying connected. The short bound prevents orphaned daemons.
            time.sleep(args.hold_seconds)
    except DevLeaseError:
        print("PROBE_HELD_OR_UNKNOWN", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
