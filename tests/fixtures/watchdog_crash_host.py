"""Disposable watchdog acceptance fixture; only run by the local test suite."""
import pathlib
import re
import sys
import time

root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "src"))
from sora_commander_next.commands import Command
from sora_commander_next.jobs import JobManager

marker = pathlib.Path(sys.argv[1])
descendant_marker = pathlib.Path(sys.argv[2])
if not marker.exists():
    # Represents a one-shot user command accepted in the original Host.
    marker.write_text("once", encoding="utf-8")
    child_code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(15)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,close_fds=True);"
        "print('DESCENDANT_PID='+str(p.pid),flush=True);time.sleep(15)"
    )
    manager = JobManager()
    result = manager.start(Command(sys.executable, ("-I", "-S", "-c", child_code), str(root)))
    session = result["session_id"]
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = manager.read(session)
        match = re.search(r"DESCENDANT_PID=(\d+)", result["stdout"])
        if match:
            descendant_marker.write_text(match.group(1), encoding="utf-8")
            break
        time.sleep(.01)
    else:
        raise RuntimeError("descendant not spawned")
else:
    # Restarted Host has zero former command/session state. Only boot the Host.
    (marker.parent / "restarted.txt").write_text("fresh", encoding="utf-8")
time.sleep(15)
