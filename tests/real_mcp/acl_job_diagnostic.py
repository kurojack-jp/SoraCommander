"""Diagnose ACL PowerShell subcommand in direct vs Windows Job Object child, read only."""
import os, subprocess, sys, tempfile
from pathlib import Path
from sora_commander_next.windows_job import WindowsJob

with tempfile.TemporaryDirectory() as folder:
    path=Path(folder)/"fixture.txt"
    path.write_text("fixture", encoding="utf8")
    script='$ErrorActionPreference="Stop"; $acl=Get-Acl -LiteralPath $env:SORA_NEXT_ACL_TARGET; if (@($acl.Access | Where-Object { -not $_.IsInherited }).Count -gt 0) { exit 3 }'
    env={**os.environ,"SORA_NEXT_ACL_TARGET":str(path)}
    cmd=["powershell.exe","-NoProfile","-NonInteractive","-Command",script]
    for mode in ("direct","job"):
        if mode=="direct":
            cp=subprocess.run(cmd,env=env,capture_output=True,timeout=15)
            print("MODE=direct RC=",cp.returncode,"STDERR=",cp.stderr.decode(errors="replace")[:600],flush=True)
        else:
            child=subprocess.Popen([sys.executable,"-c","import os,subprocess; cmd="+repr(cmd)+"; p=subprocess.run(cmd,env=os.environ,capture_output=True,timeout=15); print('NESTED_RC',p.returncode); print('NESTED_STDERR',p.stderr.decode(errors='replace')[:600])"],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=4)
            job=WindowsJob()
            try:
                job.attach_and_resume(child)
                out,err=child.communicate(timeout=20)
                print("MODE=job RC=",child.returncode,"OUT=",out.decode(errors="replace")[:1000],"ERR=",err.decode(errors="replace")[:500],flush=True)
            finally:
                job.close()
