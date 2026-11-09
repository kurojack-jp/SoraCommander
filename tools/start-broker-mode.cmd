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
set "BROKER=%~dp0start-owner-broker-dev.cmd"
set "TUNNEL=%~dp0start-sora-commander-broker-dev.cmd"
set "CONTROL_PLANE_API_KEY="
for %%F in ("%BROKER%" "%TUNNEL%") do (
    if not exist "%%~F" (
        echo [HOLD] Required local component is missing: %%~F
        set "RC=2"
        goto :finish
    )
)
if /I "%~1"=="--check" (
    call "%BROKER%" --check
    if errorlevel 1 (
        set "RC=%ERRORLEVEL%"
        goto :finish
    )
    call "%TUNNEL%" --check
    set "RC=%ERRORLEVEL%"
    goto :finish
)
rem The Broker must start before the tunnel, and only once.
powershell.exe -NoProfile -NonInteractive -Command "if(Get-Process -Name 'tunnel-client' -ErrorAction SilentlyContinue){exit 3}; if([IO.Directory]::GetFiles('\\.\pipe\') -like '*\SoraCommanderNext-Broker-*'){exit 5}; exit 0"
if errorlevel 5 (
    echo [HOLD] A Broker is already running. Start only the tunnel with start-sora-commander-broker-dev.cmd.
    set "RC=5"
    goto :finish
)
if errorlevel 3 (
    echo [HOLD] A tunnel is running. Stop it first. Nothing was started.
    set "RC=3"
    goto :finish
)
echo [INFO] Starting the Owner Broker in its own window.
start "Sora Owner Broker" cmd.exe /d /c ""%BROKER%""
powershell.exe -NoProfile -NonInteractive -Command "$end=(Get-Date).AddSeconds(30); while((Get-Date) -lt $end){ if([IO.Directory]::GetFiles('\\.\pipe\') -like '*\SoraCommanderNext-Broker-*'){exit 0}; Start-Sleep -Milliseconds 500 }; exit 4"
if errorlevel 4 (
    echo [HOLD] The Broker pipe was not ready within 30 seconds. Its window shows why. The tunnel was not started.
    set "RC=4"
    goto :finish
)
echo [INFO] The Broker pipe is present. Starting the tunnel here.
call "%TUNNEL%"
exit /b %ERRORLEVEL%

:finish
if /I not "%~1"=="--check" pause
exit /b %RC%
