@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================================
echo   Omni Shopping Agent - Local Residential IP Harvester
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
        echo [ERROR] Python 3 ne znaydeno! Vstanovit Python 3 z python.org abo Microsoft Store.
        pause
        exit /b 1
    )
)

echo [1/2] Zapusk zboru propoziciy z vashoho domashnyoho IP...
%PY_CMD% omni_residential_harvester.py --tasks relay_tasks.json --chrome

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Zbir zavershivsya z pomilkoyu. Perevirte relay_tasks.json.
    pause
    exit /b 1
)

echo.
echo [2/2] Gotovo! Vidkrivayu COPY_TO_ANTIGRAVITY.md ta papku output\ ...
if exist "output\COPY_TO_ANTIGRAVITY.md" (
    start "" notepad.exe "output\COPY_TO_ANTIGRAVITY.md"
)
if exist "output" (
    start "" explorer.exe "output"
)

echo.
echo Skopiuyte tekst z Notepad (Ctrl+A -> Ctrl+C) i vstavte v Antigravity,
echo abo peredayte fayl output\latest_relay_result.json.
pause
