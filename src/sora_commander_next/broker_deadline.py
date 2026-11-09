"""Bounded polling for a byte stream; no listener, threads or process launch."""
import time


class DeadlineExceeded(TimeoutError):
    pass


def wait_for_bytes(available, *, minimum, timeout_seconds=5.0, clock=time.monotonic, pause=time.sleep):
    """Poll an OS-backed availability callback before a bounded read.

    The transport must separately enforce a connection timeout and ensure
    no other reader can consume the bytes between peek and read.
    """
    if type(minimum) is not int or not 1 <= minimum <= 8192:
        raise ValueError("invalid minimum")
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60:
        raise ValueError("invalid timeout")
    deadline = clock() + timeout_seconds
    while True:
        n = available()
        if type(n) is not int or n < 0:
            raise ValueError("invalid availability")
        if n >= minimum:
            return n
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeadlineExceeded("pipe request deadline exceeded")
        pause(min(0.02, remaining))
