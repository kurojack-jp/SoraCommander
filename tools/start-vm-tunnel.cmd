@echo off
setlocal DisableDelayedExpansion
for %%I in ("%~dp0..") do set "BASE=%%~fI"
if not "%BASE: =%"=="%BASE%" (
    echo [HOLD] The folder path contains a space. Move Sora Commander to a folder without spaces, such as C:\SoraCommander.
    set "RC=6"
    goto :finish
)
set "PYTHON=%BASE%\.venv\Scripts\python.exe"
set "CHECK=%BASE%\tools\scn_launch_check.py"
set "SELECTOR=%BASE%\tools\scn_tunnel_client.py"
if not exist "%PYTHON%" (
    py -3.13 -I -B "%SELECTOR%" --check-checkout
    if errorlevel 7 goto :missing_python
    if errorlevel 6 (
        set "RC=6"
        goto :finish
    )
    goto :missing_python
)
"%PYTHON%" -I -B "%SELECTOR%" --check-checkout
set "RC=%ERRORLEVEL%"
if "%RC%"=="6" goto :finish
if not "%RC%"=="0" goto :unexpected
rem DEV_ONLY: starts the tunnel once with the Broker client and the VM tools as its MCP server.
rem Start tools\start-broker.cmd first. The YAML file is not changed.
rem --check: preflight only, nothing is started.
set "CONFIG=%USERPROFILE%\.config\tunnel-client\sora-next.yaml"
set "KEYFILE=%USERPROFILE%\.config\tunnel-client\sora-next.key"
set "MCP_START=%BASE%\tools\mcp-broker-client-vm.cmd"
set "KEYTOOL=%BASE%\tools\scn_tunnel_key.py"
set "CONTROL_PLANE_API_KEY="

for %%F in ("%PYTHON%" "%SELECTOR%" "%CHECK%" "%KEYTOOL%") do (
    if not exist "%%~F" (
        echo [HOLD] Required local component is missing: %%~F
        set "RC=2"
        goto :finish
    )
)
"%PYTHON%" -I -B "%KEYTOOL%" --check
set "RC=%ERRORLEVEL%"
if "%RC%"=="7" goto :query_failed
if "%RC%"=="6" goto :finish
if "%RC%"=="5" goto :finish
if "%RC%"=="4" goto :finish
if "%RC%"=="2" goto :finish
if not "%RC%"=="0" goto :unexpected
set "TUNNEL="
for /f "delims=" %%I in ('""%PYTHON%" -I -B "%SELECTOR%" --launcher-path"') do set "TUNNEL=%BASE%\%%I"
if not defined TUNNEL (
    set "RC=2"
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
"%PYTHON%" -I -B "%CHECK%" --tunnel-absent --port-free 8080
set "RC=%ERRORLEVEL%"
if "%RC%"=="7" goto :query_failed
if "%RC%"=="6" goto :finish
if "%RC%"=="3" (
    echo [HOLD] A tunnel is already running or port 8080 is in use; do not start a second tunnel.
    goto :finish
)
if not "%RC%"=="0" goto :unexpected
if /I "%~1"=="--check" (
    echo [OK] Tunnel prerequisites are present. Nothing started.
    set "RC=0"
    goto :finish
)
set "KEYREF=file:%KEYFILE:\=/%"
set "MCP_FORWARD=%MCP_START:\=/%"
echo [INFO] Single tunnel start with the Broker client and the VM tools. No automatic retry.
"%TUNNEL%" run --config "%CONFIG%" --control-plane.api-key "%KEYREF%" --mcp.command "cmd.exe /d /c %MCP_FORWARD%"
set "RC=%ERRORLEVEL%"
echo [INFO] Tunnel finished with code %RC%.
goto :finish

:missing_python
echo [HOLD] Development virtual environment not found: "%PYTHON%"
goto :query_failed

:unexpected
echo [HOLD] Launch check returned unexpected exit code %RC%.
:query_failed
set "RC=2"

:finish
if not "%RC%"=="6" if /I not "%~1"=="--check" pause
exit /b %RC%
