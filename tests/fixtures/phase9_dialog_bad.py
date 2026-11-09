"""Test-only incorrect challenge; must never approve."""
import json,sys
p=json.loads(sys.stdin.buffer.readline(131073))
sys.stdout.write(json.dumps({"nonce":"0"*64,"decision":"APPROVE"})+"\n")
sys.stdout.flush()
