"""Synchronous compatibility facade over bounded direct-child supervision."""

from .commands import Command
from .jobs import JobManager


def execute(request: Command) -> dict:
    manager = JobManager(max_concurrent=1, capacity=1)
    try:
        started = manager.start(request)
        return manager.wait(started["session_id"])
    finally:
        manager.close()
