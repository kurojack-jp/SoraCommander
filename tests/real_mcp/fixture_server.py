"""Disposable test-only server; never imported by the installed package."""

from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from sora_commander_next.host import Host
from sora_commander_next.mcp_adapter import serve_stdio
from sora_commander_next.policy import Grant, Pattern, Policy


def build_host():
    # Exact benign interpreter invocations only. No script, shell or remote grant.
    policy = Policy((str(ROOT / "tests"),), (Pattern(sys.executable, ("--version",)),))
    policy.add_grant_local(Grant(
        "phase3-fixed-failure", "test-owner", str(ROOT / "tests"),
        (Pattern(sys.executable, ("--sora-phase3-invalid-option",)),), monotonic() + 60,
    ))
    return Host(policy, "test-owner")


def main():
    serve_stdio(build_host())


if __name__ == "__main__":
    main()
