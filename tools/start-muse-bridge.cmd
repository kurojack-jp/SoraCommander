@echo off
setlocal
rem Sora Commander read-only bridge for Muse.
rem No personal values in this file:
rem - the checkout is derived from this script's own location (tools\..)
rem - the bind address is this PC's Tailscale IPv4, detected at startup via
rem   the tailscale CLI (PATH first, then the default install location).
rem   Fallback only: if detection fails, one line in
rem   %USERPROFILE%\.config\sora-commander\bridge-bind.txt is used.
set "CHECKOUT=%~dp0.."
set "TSCMD="
where /q tailscale.exe 2>nul && set "TSCMD=tailscale"
if not defined TSCMD if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TSCMD=%ProgramFiles%\Tailscale\tailscale.exe"
set "BIND="
if defined TSCMD (
  for /f "delims=" %%A in ('"%TSCMD%" ip -4 2^>nul') do set "BIND=%%A"
)
rem Set outside the block: %BINDFILE% inside a ( ) block expands before the block runs.
set "BINDFILE=%USERPROFILE%\.config\sora-commander\bridge-bind.txt"
if not defined BIND (
  if exist "%BINDFILE%" set /p BIND=<"%BINDFILE%"
)
if not defined BIND (
  echo Could not determine the Tailscale IPv4 address. Is Tailscale running?
  pause
  exit /b 1
)
set "PYEXE=%CHECKOUT%\.venv\Scripts\python.exe"
if exist "%PYEXE%" (
  "%PYEXE%" "%CHECKOUT%\tools\scn_muse_bridge.py" --checkout "%CHECKOUT%" --bind "%BIND%" --port 8765 %*
) else (
  py -3.13 "%CHECKOUT%\tools\scn_muse_bridge.py" --checkout "%CHECKOUT%" --bind "%BIND%" --port 8765 %*
)
pause
