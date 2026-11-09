"""Opt-in product Host composition. No native trust implementation is accepted.

This entrypoint cannot be armed with asserted JSON, Boolean flags, SCM-only
observations, SQLite fixture receipts or injected development components.
Future native admission requires a separately reviewed implementation change.
"""
from .broker_host import BrokerHost, UnavailableTransport


def build_product_host(*, read_roots=()):
    # Deliberately no transport/service/profile/Owner proof parameter. B04-a
    # reports NOT_AUTHENTICATED; there is no positive native verifier to call.
    return BrokerHost(UnavailableTransport(), read_roots=read_roots)
