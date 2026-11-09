@echo off
setlocal DisableDelayedExpansion
for %%I in ("%~dp0..") do set "BASE=%%~fI"
if not "%BASE: =%"=="%BASE%" (
    echo [HOLD] The folder path contains a space. Move Sora Commander to a folder without spaces, such as C:\SoraCommander.
    set "RC=6"
    goto :finish
)
rem DEV_ONLY: starts the Owner Broker once. Stop it with Ctrl+C. No automatic restart.
rem --check: preflight only, nothing is started.
set "SORA_NEXT_OWNER_REVIEW_PILOT=1"
set "SORA_NEXT_OWNER_PROFILE=development"
set "SORA_NEXT_JOURNAL_PILOT=1"
set "SORA_NEXT_RESULT_ARCHIVE_PILOT=1"
set "SORA_NEXT_EDIT_PILOT=1"
set "SORA_NEXT_CREATE_PILOT=1"
set "CONTROL_PLANE_API_KEY="
if not exist "%BASE%\.venv\Scripts\python.exe" (
    echo [HOLD] Development virtual environment not found: %BASE%\.venv
    set "RC=2"
    goto :finish
)
rem The usual Host keeps the same job journal, so the two must never run together.
powershell.exe -NoProfile -NonInteractive -Command "if(Get-Process -Name 'tunnel-client' -ErrorAction SilentlyContinue){exit 3}; exit 0"
if errorlevel 3 (
    echo [HOLD] A tunnel is running. Stop it first, then start the Broker before the tunnel.
    set "RC=3"
    goto :finish
)
if /I "%~1"=="--check" (
    echo [OK] Broker prerequisites are present. Nothing started.
    set "RC=0"
    goto :finish
)
set "PYTHONPATH=%BASE%\src"
"%BASE%\.venv\Scripts\python.exe" -m sora_commander_next.owner_broker_main
set "RC=%ERRORLEVEL%"
echo [INFO] Broker finished with code %RC%.

:finish
if /I not "%~1"=="--check" pause
exit /b %RC%
