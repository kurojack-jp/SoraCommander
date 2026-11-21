@echo off
setlocal DisableDelayedExpansion
py -3.13 -I -B "%~dp0scn_setup.py" %*
if %ERRORLEVEL% EQU 0 exit /b 0
if %ERRORLEVEL% EQU 6 exit /b 6
exit /b 2
