"""Test child: open an independent ledger, then reserve at the shared signal."""
from pathlib import Path
import sys
import time

from sora_commander_next.commands import Command
from sora_commander_next.owner_launch_ledger import LaunchLedger


def main():
    ledger = LaunchLedger(sys.argv[1])
    try:
        request = Command(sys.executable, ("-c", "print('ok')"), sys.argv[2],
                          timeout=30, output_limit=1024, request_id=sys.argv[3])
        signal = Path(sys.argv[4])
        print("READY", flush=True)
        deadline = time.monotonic() + 20
        while not signal.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("reserve signal did not arrive")
            time.sleep(0.005)
        print(ledger.reserve(request), flush=True)
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
