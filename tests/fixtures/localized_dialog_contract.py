"""Test-only private-pipe stand-in, NOT a real user click. Never opens a window.

The test's harmless command text names the scenario, so one fixture covers every
reply. A frame with a locale goes through the real localized parser.
"""
import importlib.util
import json
from pathlib import Path
import re
import sys
import time

raw = sys.stdin.buffer.readline(131073)
frame = json.loads(raw)
match = re.search(r"scenario:([A-Z_]+):(en|ja|none)", " ".join(frame["argv"]))
scenario, expected = match.group(1), match.group(2)
if expected != "none":
    location = Path(__file__).resolve().parents[2] / "src" / "sora_commander_next" / \
        "owner_command_dialog_localized.py"
    spec = importlib.util.spec_from_file_location("_real_localized_parser", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    frame = module.parse_request(raw)
    if frame["locale"] != expected or frame["seconds"] > 45:
        sys.exit(9)
reply = {"nonce": frame["nonce"], "decision": scenario}
out = sys.stdout.buffer
if scenario == "SLOW":
    time.sleep(.5)
    reply["decision"] = "APPROVE"
elif scenario == "HANG":
    time.sleep(30)
elif scenario == "BAD_NONCE":
    reply = {"nonce": "b" * 64, "decision": "APPROVE"}
elif scenario == "EXTRA_FIELD":
    reply = {"nonce": frame["nonce"], "decision": "APPROVE", "locale": "ja"}
elif scenario == "UNKNOWN_DECISION":
    reply["decision"] = "TRUSTED"
elif scenario == "NO_REPLY":
    sys.exit(0)
elif scenario == "GARBAGE":
    out.write(b"not json\n")
    sys.exit(0)
elif scenario == "HUGE":
    out.write(json.dumps({**reply, "decision": "APPROVE", "pad": "x" * 600}).encode() + b"\n")
    sys.exit(0)
elif scenario == "TWO_REPLIES":
    reply["decision"] = "APPROVE"
    out.write(json.dumps(reply).encode() + b"\n")
elif scenario == "CRASH_AFTER_APPROVE":
    reply["decision"] = "APPROVE"
    out.write(json.dumps(reply).encode() + b"\n")
    out.flush()
    sys.exit(3)
out.write(json.dumps(reply).encode() + b"\n")
out.flush()
