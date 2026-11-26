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
set "SORA_NEXT_VM_TOOLS="
set "CONTROL_PLANE_API_KEY="
set "PYTHON=%BASE%\.venv\Scripts\python.exe"
set "CHECK=%BASE%\tools\scn_launch_check.py"
for %%F in ("%PYTHON%" "%CHECK%") do (
    if not exist "%%~F" (
        echo [HOLD] Required local component is missing: %%~F
        set "RC=2"
        goto :finish
    )
)
rem The usual Host keeps the same job journal, so the two must never run together.
"%PYTHON%" -I -B "%CHECK%" --tunnel-absent
set "RC=%ERRORLEVEL%"
if "%RC%"=="7" goto :query_failed
if "%RC%"=="6" goto :finish
if "%RC%"=="3" (
    echo [HOLD] A tunnel is running. Stop it first, then start the Broker before the tunnel.
    goto :finish
)
if not "%RC%"=="0" goto :unexpected
if /I "%~1"=="--check" (
    echo [OK] Broker prerequisites are present. Nothing started.
    set "RC=0"
    goto :finish
)
set "PYTHONPATH=%BASE%\src"
"%BASE%\.venv\Scripts\python.exe" -m sora_commander_next.owner_broker_main
set "RC=%ERRORLEVEL%"
echo [INFO] Broker finished with code %RC%.
goto :finish

:unexpected
echo [HOLD] Launch check returned unexpected exit code %RC%.
:query_failed
set "RC=2"

:finish
if /I not "%~1"=="--check" pause
exit /b %RC%
