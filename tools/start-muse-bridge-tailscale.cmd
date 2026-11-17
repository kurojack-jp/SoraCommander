@echo off
setlocal
rem Sora Commander read-only bridge for Muse, Tailscale node identity only.
rem Muse connects without a token; only nodes in the "readonly" list of
rem %USERPROFILE%\.config\sora-commander\muse-bridge-peers.json get in.
rem The read-only bridge opens no Owner journal, so it can run beside the Broker.
rem No personal values in this file; see start-muse-bridge.cmd for the address.
call "%~dp0start-muse-bridge.cmd" --auth tailscale %*
exit /b %ERRORLEVEL%
