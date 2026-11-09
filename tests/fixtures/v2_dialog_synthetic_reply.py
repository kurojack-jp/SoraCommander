"""Synthetic v2 UI PIPE FIXTURE ONLY, no Tk/no Owner decision.

Writes the exact protocol fields for tests. NEVER distribute or call as an
Owner authorization dialog; the real v2 pilot always requires a human click.
"""
import json
import os
import sys

raw = sys.stdin.buffer.readline(65537)
review = json.loads(raw.decode("utf-8"))
choice = os.getenv("SORA_V2_TEST_CHOICE", "ONCE")
if choice == "BAD_NONCE":
    nonce = "f" * 64
    choice = "ONCE"
else:
    nonce = review["nonce"]
response = {
    "nonce": nonce,
    "review_id": review["review_id"],
    "spec_sha256": review["spec_sha256"],
    "decision": choice,
}
sys.stdout.buffer.write(
    (json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii"))
sys.stdout.buffer.flush()
