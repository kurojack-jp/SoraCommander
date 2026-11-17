"""Test-process-only lease namespace; never imported by production entry points."""
from contextlib import contextmanager
import os
import re
import secrets
from unittest.mock import patch


def new_test_prefix():
    return ("Local\\SoraCommanderNext.BrokerOwnerLeaseTest.v1."
            + str(os.getpid()) + "." + secrets.token_hex(16) + ".")


@contextmanager
def isolated_owner_lease(prefix=None):
    """Patch construction names, restoring the previous prefix after cleanup.

    Pass the yielded prefix explicitly to fixed child test code via argv.
    No environment, production constructor or production CLI override is used.
    """
    if prefix is None:
        prefix = new_test_prefix()
    if not isinstance(prefix, str) or re.fullmatch(
            r"Local\\SoraCommanderNext\.BrokerOwnerLeaseTest\.v1\.[0-9]+\.[0-9a-f]{32}\.",
            prefix) is None:
        raise ValueError("test-only Owner lease prefix required")
    from sora_commander_next import broker_owner_lease
    with patch.object(broker_owner_lease, "_LEASE_NAME_PREFIX", prefix):
        yield prefix
