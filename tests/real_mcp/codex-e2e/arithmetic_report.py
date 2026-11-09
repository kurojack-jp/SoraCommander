"""Report arithmetic results using the counter helpers."""

from counter import add, subtract


def describe(a, b):
    """Return the sum and difference of a and b."""
    return {"sum": add(a, b), "difference": subtract(a, b)}
