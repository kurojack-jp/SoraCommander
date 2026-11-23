@echo off
setlocal
set "SORA_NEXT_OWNER_REVIEW_PILOT=1"
set "SORA_NEXT_OWNER_PROFILE=development"
set "SORA_NEXT_JOURNAL_PILOT=1"
set "SORA_NEXT_RESULT_ARCHIVE_PILOT=1"
set "SORA_NEXT_EDIT_PILOT=1"
set "SORA_NEXT_CREATE_PILOT=1"
for %%I in ("%~dp0..") do set "BASE=%%~fI"
if not exist "%BASE%\.venv\Scripts\python.exe" (
    echo [HOLD] Development virtual environment not found: %BASE%\.venv
    exit /b 2
)
set "PYTHONPATH=%BASE%\src"
"%BASE%\.venv\Scripts\python.exe" -m sora_commander_next.stdio_main
exit /b %ERRORLEVEL%
