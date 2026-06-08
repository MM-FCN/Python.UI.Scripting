@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "EDGE_EXE="
if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
    set "EDGE_EXE=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
) else if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    set "EDGE_EXE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
)

if "%EDGE_EXE%"=="" (
    echo [ERROR] Microsoft Edge not found in default install paths.
    echo [INFO] Please edit this file and set EDGE_EXE manually.
    pause
    exit /b 1
)

set "PROFILE_DIR=C:\MyEdgeData2"
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"

set "TARGET_URL=https://www.cma-cgm.com"
if not "%~1"=="" set "TARGET_URL=%~1"

echo [INFO] Starting Edge (CMA) with remote debugging port 9222...
echo [INFO] Edge: %EDGE_EXE%
echo [INFO] User data dir: %PROFILE_DIR%
echo [INFO] Target URL: %TARGET_URL%
start "" "%EDGE_EXE%" --remote-debugging-port=9222 --user-data-dir="%PROFILE_DIR%" --new-window --start-maximized "%TARGET_URL%"

echo.
echo [INFO] Edge should now listen on: http://127.0.0.1:9222/json/version
echo [INFO] Crawling flow is handled by main polling mode.
echo.
exit /b 0
