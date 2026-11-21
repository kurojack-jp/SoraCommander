@echo off
setlocal
if not exist "%~dp0..\.venv\Scripts\python.exe" (
    echo [HOLD] Run tools\setup.cmd first ^(.venv is missing^).
    pause
    exit /b 2
)
"%~dp0..\.venv\Scripts\python.exe" -I -B "%~dp0scn_muse_autostart.py" uninstall
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
