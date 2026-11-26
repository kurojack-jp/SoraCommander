@echo off
setlocal DisableDelayedExpansion
if /i "%~1"=="/in-conhost" goto :run
start "" "%SystemRoot%\System32\conhost.exe" "%ComSpec%" /d /s /c ""%~f0" /in-conhost"
exit /b %errorlevel%
:run
set "PY=%ProgramFiles%\Python313\python.exe"
set "SCRIPT=%~dp0vm_guest.py"
setlocal EnableDelayedExpansion
(
  set "CODE=1"
  if not exist "!PY!" (
    echo NG: Python 3.13 is required. Nothing was done.
  ) else (
    "!PY!" -E -s -B "!SCRIPT!" start-view
    set "CODE=!errorlevel!"
  )
  echo exit code !CODE!
  pause
  exit /b !CODE!
)
