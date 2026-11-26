@echo off
setlocal DisableDelayedExpansion
set "PY=%ProgramFiles%\Python313\python.exe"
set "SCRIPT=%~dp0vm_guest.py"
setlocal EnableDelayedExpansion
(
  set "CODE=1"
  if not exist "!PY!" (
    echo NG: Python 3.13 is required. Nothing was done.
  ) else (
    "!PY!" -E -s -B "!SCRIPT!" firewall-remove-admin
    set "CODE=!errorlevel!"
  )
  echo exit code !CODE!
  pause
  exit /b !CODE!
)
