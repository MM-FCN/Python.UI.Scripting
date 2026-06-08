@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo.
echo ============================================================
echo  Stop existing crawler processes
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$killed=0;" ^
  "Get-CimInstance Win32_Process ^| Where-Object { ($_.Name -ieq 'python.exe' -or $_.Name -ieq 'pythonw.exe') -and $_.CommandLine -match 'src\.main' } ^| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $killed++ };" ^
  "Get-CimInstance Win32_Process ^| Where-Object { $_.Name -ieq 'msedgedriver.exe' } ^| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue };" ^
  "Write-Host ('[INFO] Killed crawler python processes: ' + $killed)"

if "%~1"=="" (
    set "RUN_ARGS=--watch-input --watch-sites cargonavi --watch-interval 10"
) else (
    set "RUN_ARGS=%*"
)

echo.
echo ============================================================
echo  Start crawler
echo ============================================================
echo [INFO] Python: %PYTHON_EXE%
echo [INFO] Args  : %RUN_ARGS%
echo.

"%PYTHON_EXE%" -m src.main %RUN_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if %EXIT_CODE%==0 (
    echo [SUCCESS] Process exited normally.
) else (
    echo [FAILED] Process exited with code: %EXIT_CODE%
)

pause
exit /b %EXIT_CODE%
