"""Phase10H local benchmark: repeatable observations, no deployment or background monitor."""
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
from time import perf_counter_ns

from .commands import Command
from .host import Host
from .policy import Decision, Grant, Pattern, Policy

def measure(iterations=10000):
    if type(iterations) is not int or not 100 <= iterations <= 100000:
        raise ValueError("bounded benchmark iterations required")
    with tempfile.TemporaryDirectory(prefix="sora-10h-") as root:
        executable=str(Path(sys.executable).resolve())
        request=Command(executable=executable,argv=("--version",),cwd=root)
        policy=Policy(roots=(root,),auto=(Pattern(executable,("--version",)),))
        policy.add_grant_local(Grant("bench","owner",root,(Pattern(executable,("--version",)),),
                                     policy.clock()+600))
        host=Host(policy=policy,owner="owner")
        durations=[]
        try:
            start=perf_counter_ns()
            for _ in range(iterations):
                tick=perf_counter_ns()
                if policy.classify(request,"owner") != Decision.SCOPED:
                    raise RuntimeError("grant unexpectedly failed")
                durations.append(perf_counter_ns()-tick)
            total=perf_counter_ns()-start
            return {
                "iterations":iterations,
                "scoped_median_us":round(statistics.median(durations)/1000,3),
                "scoped_p95_us":round(sorted(durations)[int(iterations*.95)]/1000,3),
                "loop_total_ms":round(total/1e6,3),
                "host_ping":host.ping()["status"],
                "process_pid":os.getpid(),
                "note":"single foreground process; no idle CPU or installed-service measurement",
            }
        finally:
            host.jobs.close()

if __name__=="__main__":
    print(json.dumps(measure(),sort_keys=True))
