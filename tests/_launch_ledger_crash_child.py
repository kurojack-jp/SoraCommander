"""Test child: starts one harmless long job, reports, then dies with no cleanup."""
import os
import sys

from sora_commander_next.commands import Command
from sora_commander_next.owner_launch_ledger import DurableJobManager, LaunchLedger


def main():
    ledger = LaunchLedger(sys.argv[1])
    manager = DurableJobManager(ledger=ledger)
    result = manager.start(Command(
        sys.executable, ("-c", "import time; time.sleep(60)"), sys.argv[2],
        timeout=90, output_limit=1024, request_id=sys.argv[3]))
    print(result.get("status"), flush=True)
    os._exit(9)


if __name__ == "__main__":
    main()
