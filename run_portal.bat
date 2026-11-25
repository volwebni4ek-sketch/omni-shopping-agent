@echo off
chcp 65001 > nul
setlocal

cd /d "%~dp0"

where uv >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] "uv" is not installed or not in PATH.
    echo Install uv: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)

echo [omni-agent] Starting local HITL Portal at http://127.0.0.1:8765 ...
start "" http://127.0.0.1:8765
uv run omni-agent serve --host 127.0.0.1 --port 8765

endlocal
