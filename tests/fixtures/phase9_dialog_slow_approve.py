"""Synthetic delayed private pipe response; NEVER the actual Owner Tk dialog."""
import json,sys,time
p=json.loads(sys.stdin.buffer.readline(131073))
time.sleep(1.0)
sys.stdout.write(json.dumps({"nonce":p["nonce"],"decision":"APPROVE"})+"\n")
sys.stdout.flush()
