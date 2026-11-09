"""Test-only wire-format oracle, independent of production release code."""
import json


def canonical_json_wire(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":")) + "\n").encode("ascii")
