@echo off
setlocal DisableDelayedExpansion
for %%I in ("%~dp0..") do set "BASE=%%~fI"
if not "%BASE: =%"=="%BASE%" (
    echo [HOLD] The folder path contains a space. Move Sora Commander to a folder without spaces, such as C:\SoraCommander.
    set "RC=6"
    goto :finish
)
rem DEV_ONLY: starts the tunnel once with the Broker client as its MCP server.
rem Start tools\start-owner-broker-dev.cmd first. The YAML file is not changed.
rem --check: preflight only, nothing is started.
set "TUNNEL=%BASE%\tools\tunnel-client\v0.0.16\tunnel-client.exe"
set "CONFIG=%USERPROFILE%\.config\tunnel-client\sora-next.yaml"
set "KEYFILE=%USERPROFILE%\.config\tunnel-client\sora-next.key"
set "MCP_START=%BASE%\tools\next-development-broker-client.cmd"
set "KEYTOOL=%BASE%\tools\Set-NextTunnelKey.ps1"
set "CONTROL_PLANE_API_KEY="

if not exist "%KEYTOOL%" (
    echo [HOLD] Required local component is missing: %KEYTOOL%
    set "RC=2"
    goto :finish
)
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%KEYTOOL%" -Check
if errorlevel 1 (
    set "RC=%ERRORLEVEL%"
    goto :finish
)
for %%F in ("%TUNNEL%" "%CONFIG%" "%MCP_START%") do (
    if not exist "%%~F" (
        echo [HOLD] Required local component is missing: %%~F
        set "RC=2"
        goto :finish
    )
)
rem Fail closed if a tunnel is running or its local health port cannot be taken.
powershell.exe -NoProfile -NonInteractive -Command "if(Get-Process -Name 'tunnel-client' -ErrorAction SilentlyContinue){exit 3}; $l=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,8080); try{$l.Start();$l.Stop()}catch{exit 3}; exit 0"
if errorlevel 3 (
    echo [HOLD] A tunnel is already running or port 8080 is in use; do not start a second tunnel.
    set "RC=3"
    goto :finish
)
if /I "%~1"=="--check" (
    echo [OK] Tunnel prerequisites are present. Nothing started.
    set "RC=0"
    goto :finish
)
set "KEYREF=file:%KEYFILE:\=/%"
set "MCP_FORWARD=%MCP_START:\=/%"
echo [INFO] Single tunnel start with the Broker client. No automatic retry.
"%TUNNEL%" run --config "%CONFIG%" --control-plane.api-key "%KEYREF%" --mcp.command "cmd.exe /d /c %MCP_FORWARD%"
set "RC=%ERRORLEVEL%"
echo [INFO] Tunnel finished with code %RC%.

:finish
if /I not "%~1"=="--check" pause
exit /b %RC%
