"""Process-entry lifetime guard; no acquisition or Windows import at import time."""
from contextlib import contextmanager


OWNER_JOURNAL_HELD = (
    "another Owner Broker, stdio pilot or exec bridge holds the Owner journal")


class OwnerJournalLeaseError(RuntimeError):
    """Sanitized startup refusal; underlying identity/OS details stay local."""


def _new_lease():
    from .broker_owner_lease import BrokerOwnerLease
    return BrokerOwnerLease()


@contextmanager
def owner_journal_lease(enabled=True):
    """Acquire once and close on the caller thread, outside server event loops.

    Only acquisition failures are translated; cleanup failures must propagate.
    No alternate name, retry, waiting, ACL change or lease reuse.
    """
    if not enabled:
        yield
        return
    try:
        lease = _new_lease()
        lease.__enter__()
    except PermissionError:
        raise OwnerJournalLeaseError(OWNER_JOURNAL_HELD) from None
    try:
        yield
    finally:
        lease.close()
