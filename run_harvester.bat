@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

where uv >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] "uv" is not installed or not in PATH.
    echo Install uv: powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)

if exist ".git" (
    echo [omni-agent] Updating repository (git pull --ff-only)...
    git pull --ff-only
)

echo [omni-agent] Running harvest queue...
uv run omni-agent run-queue --export-dir reports %*
if %errorlevel% neq 0 (
    echo [ERROR] Harvester exited with code %errorlevel%.
    pause
    exit /b %errorlevel%
)

set "SILENT_MODE=0"
for %%A in (%*) do (
    if /I "%%~A"=="--silent" set "SILENT_MODE=1"
)

if "%SILENT_MODE%"=="0" (
    if exist "reports\latest_report.html" (
        echo [omni-agent] Opening reports\latest_report.html...
        start "" "reports\latest_report.html"
    )
)

endlocal
