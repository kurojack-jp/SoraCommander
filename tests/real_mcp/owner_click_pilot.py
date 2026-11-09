"""Explicit disposable owner-click pilot: no automatic approval or external endpoint."""
from pathlib import Path
import sys
from time import sleep
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"src"))
from sora_commander_next.host import Host
from sora_commander_next.policy import Policy
from sora_commander_next.windows_owner import OwnerSessionGuard
from sora_commander_next.windows_owner_controller import WindowsOwnerController
from sora_commander_next.owner_review_dispatch import OwnerReviewDispatcher

def main():
    guard=OwnerSessionGuard()
    guard.verify()
    host=Host(policy=Policy(roots=(str(ROOT/"tests"),)),owner=guard.sid)
    try:
        dispatcher=OwnerReviewDispatcher(WindowsOwnerController(host))
        host.attach_owner_review_local(dispatcher)
        result=host.run_command(executable=sys.executable,argv=["--version"],cwd=str(ROOT/"tests"))
        print("PILOT_REQUEST="+result["status"],flush=True)
        for _ in range(120):
            answer=dispatcher.result_local()
            if answer is not None:
                print("OWNER_RESULT="+answer["status"],flush=True)
                if answer["status"] == "RUNNING":
                    session_id=dispatcher.job_local()
                    if session_id is None:
                        raise RuntimeError("approved session missing")
                    completed=host.jobs.wait(session_id)
                    print("COMMAND_STATUS="+completed["status"],flush=True)
                    print("COMMAND_EXIT="+str(completed.get("exit_code")),flush=True)
                    print("COMMAND_STDOUT="+str(completed.get("stdout","")).strip(),flush=True)
                    host._owner_review_dispatcher=None  # test only: second request must not open another dialog
                    repeat=host.run_command(executable=sys.executable,argv=["--version"],cwd=str(ROOT/"tests"))
                    print("REPEAT_STATUS="+repeat["status"],flush=True)
                break
            sleep(.25)
        else:
            print("OWNER_RESULT=TIMEOUT",flush=True)
    finally:
        host.jobs.close()
if __name__=="__main__":main()
