@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================================
echo   Omni Shopping Agent - Auto-Sync Residential Harvester
echo ========================================================
echo.

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PY_CMD=py -3"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 (
        set "PY_CMD=python"
    ) else (
        echo [ERROR] Python 3 ne znaydeno! Vstanovit: winget install -e --id Python.Python.3.12
        pause
        exit /b 1
    )
)

REM Auto-pull latest relay_tasks.json and omni_residential_harvester.py from cloud unless --no-update passed
if /I not "%~1"=="--no-update" (
    where curl >nul 2>nul
    if %ERRORLEVEL%==0 (
        echo [0/3] Avto-onovlennya relay_tasks.json ta skriptu z хmari...
        curl -fsSL "https://drive.google.com/uc?export=download&id=1_KfUOCAsrZEF2MjJGicMZCKWbHAZd3-v" -o "relay_tasks.json.tmp" 2>nul
        if exist "relay_tasks.json.tmp" (
            for %%A in ("relay_tasks.json.tmp") do if %%~zA GTR 100 move /y "relay_tasks.json.tmp" "relay_tasks.json" >nul
            if exist "relay_tasks.json.tmp" del /q "relay_tasks.json.tmp" >nul
        )
        curl -fsSL "https://raw.githubusercontent.com/volwebni4ek-sketch/omni-shopping-agent/main/local_relay/omni_residential_harvester.py" -o "omni_residential_harvester.py.tmp" 2>nul
        if exist "omni_residential_harvester.py.tmp" (
            for %%A in ("omni_residential_harvester.py.tmp") do if %%~zA GTR 5000 move /y "omni_residential_harvester.py.tmp" "omni_residential_harvester.py" >nul
            if exist "omni_residential_harvester.py.tmp" del /q "omni_residential_harvester.py.tmp" >nul
        )
    )
)

set "TASKS_FILE=relay_tasks.json"
if not "%~1"=="" if /I not "%~1"=="--no-update" set "TASKS_FILE=%~1"

echo [1/3] Zapusk zboru z vashoho domashnyoho IP (%TASKS_FILE%)...
%PY_CMD% omni_residential_harvester.py --tasks "%TASKS_FILE%" --chrome

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Zbir zavershivsya z pomilkoyu. Perevirte %TASKS_FILE%.
    pause
    exit /b 1
)

echo.
echo [2/3] Gotovo! Vidkrivayu COPY_TO_ANTIGRAVITY.md ta papku output\ ...
if exist "output\COPY_TO_ANTIGRAVITY.md" (
    start "" notepad.exe "output\COPY_TO_ANTIGRAVITY.md"
)
if exist "output" (
    start "" explorer.exe "output"
)

echo [3/3] Yakshcho tse sinkhronizovana papka Google Drive, output\ vzhe letit u khmaru!
pause
