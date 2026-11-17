"""Test-only Broker entry: first argv is a shared test lease prefix."""
import sys

from tests._test_owner_lease import isolated_owner_lease


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        raise ValueError("test Owner lease prefix required")
    with isolated_owner_lease(arguments[0]):
        from sora_commander_next.owner_broker_main import main as broker_main
        return broker_main(arguments[1:])


if __name__ == "__main__":
    sys.exit(main())
