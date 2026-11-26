@echo off
setlocal DisableDelayedExpansion
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [HOLD] Set up the checkout Python environment first.
  exit /b 2
)
set "PYTHONPATH=%~dp0..\src"
"%PY%" -s -B "%~dp0scn_vm_setup.py" %*
exit /b %errorlevel%
