"""Test-only private-pipe stand-in, NOT a real user click."""
import json,sys,time
p=json.loads(sys.stdin.buffer.readline(131073))
time.sleep(.08)
sys.stdout.write(json.dumps({"nonce":p["nonce"],"decision":"DENY"})+"\n")
sys.stdout.flush()
