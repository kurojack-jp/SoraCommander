@echo off
setlocal
"%~dp0..\.venv\Scripts\python.exe" -I -B "%~dp0scn_muse_autostart.py" uninstall
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
