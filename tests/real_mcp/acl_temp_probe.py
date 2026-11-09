"""Read-only ACL diagnostics for a newly created temporary fixture. No secrets."""
import os
import subprocess
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as root:
    p=Path(root)/"probe.txt"
    p.write_bytes(b"probe")
    script='$ErrorActionPreference="Stop"; $a=Get-Acl -LiteralPath $env:SORA_NEXT_ACL_TARGET; $explicit=@($a.Access | Where-Object { -not $_.IsInherited }); Write-Output ("EXPLICIT_ACES="+$explicit.Count); Write-Output ("ACL_PROTECTED="+$a.AreAccessRulesProtected); if($explicit.Count -gt 0){exit 3}'
    r=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",script],capture_output=True,timeout=15,env={**os.environ,"SORA_NEXT_ACL_TARGET":str(p)})
    print("TEMP_BASE="+str(Path(tempfile.gettempdir())),flush=True)
    print("PSMODULEPATH="+ascii(os.environ.get("PSModulePath","<MISSING>")),flush=True)
    print("SYSTEMROOT="+ascii(os.environ.get("SystemRoot","<MISSING>")),flush=True)
    print("ACL_RC="+str(r.returncode),flush=True)
    print("ACL_STDOUT="+r.stdout.decode("utf-8",errors="replace").strip(),flush=True)
    print("ACL_STDERR_ASCII="+ascii(r.stderr.decode("cp932",errors="replace").strip()[:800]),flush=True)
