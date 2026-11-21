@echo off
setlocal DisableDelayedExpansion
if exist "%~dp0..\.venv\Scripts\python.exe" goto run
py -3.13 -I -B "%~dp0scn_tunnel_client.py" --check-checkout
if %ERRORLEVEL% EQU 6 exit /b 6
echo [HOLD] Run tools\setup.cmd to prepare the virtual environment.
exit /b 2
:run
"%~dp0..\.venv\Scripts\python.exe" -I -B "%~dp0scn_tunnel_key.py" %*
exit /b %ERRORLEVEL%
