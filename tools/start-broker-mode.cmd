@echo off
setlocal DisableDelayedExpansion
for %%I in ("%~dp0..") do set "BASE=%%~fI"
if not "%BASE: =%"=="%BASE%" (
    echo [HOLD] The folder path contains a space. Move Sora Commander to a folder without spaces, such as C:\SoraCommander.
    set "RC=6"
    goto :finish
)
rem DEV_ONLY: Broker mode in one step. One manual start, no automatic retry.
rem Starts the Owner Broker in its own window, waits until its pipe is present,
rem then starts the tunnel in this window. To stop: Ctrl+C here, then in the Broker window.
rem --check: runs both launchers' preflight only, nothing is started.
set "BROKER=%~dp0start-broker.cmd"
set "TUNNEL=%~dp0start-broker-tunnel.cmd"
set "CONTROL_PLANE_API_KEY="
set "PYTHON=%BASE%\.venv\Scripts\python.exe"
set "CHECK=%BASE%\tools\scn_launch_check.py"
for %%F in ("%PYTHON%" "%CHECK%" "%BROKER%" "%TUNNEL%") do (
    if not exist "%%~F" (
        echo [HOLD] Required local component is missing: %%~F
        set "RC=2"
        goto :finish
    )
)
if /I "%~1"=="--check" goto :check_only
rem The Broker must start before the tunnel, and only once.
"%PYTHON%" -I -B "%CHECK%" --tunnel-absent --broker-absent
set "RC=%ERRORLEVEL%"
if "%RC%"=="7" goto :query_failed
if "%RC%"=="6" goto :finish
if "%RC%"=="5" (
    echo [HOLD] A Broker is already running. Start only the tunnel with start-broker-tunnel.cmd.
    goto :finish
)
if "%RC%"=="3" (
    echo [HOLD] A tunnel is running. Stop it first. Nothing was started.
    goto :finish
)
if not "%RC%"=="0" goto :unexpected
echo [INFO] Starting the Owner Broker in its own window.
start "Sora Owner Broker" cmd.exe /d /c ""%BROKER%""
"%PYTHON%" -I -B "%CHECK%" --wait-broker 30
set "RC=%ERRORLEVEL%"
if "%RC%"=="7" goto :query_failed
if "%RC%"=="6" goto :finish
if "%RC%"=="4" (
    echo [HOLD] The Broker pipe was not ready within 30 seconds. Its window shows why. The tunnel was not started.
    goto :finish
)
if not "%RC%"=="0" goto :unexpected
echo [INFO] The Broker pipe is present. Starting the tunnel here.
call "%TUNNEL%"
exit /b %ERRORLEVEL%

:check_only
call "%BROKER%" --check
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :finish
call "%TUNNEL%" --check
set "RC=%ERRORLEVEL%"
goto :finish

:unexpected
echo [HOLD] Launch check returned unexpected exit code %RC%.
:query_failed
set "RC=2"

:finish
if /I not "%~1"=="--check" pause
exit /b %RC%
