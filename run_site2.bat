@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================================
echo  运行 site2 爬取流程
echo ============================================================
echo.

.venv\Scripts\python -m src.main --site site2
set EXIT_CODE=%ERRORLEVEL%

echo.
if %EXIT_CODE%==0 (
    echo [SUCCESS] site2 执行成功
) else (
    echo [FAILED]  site2 执行失败，退出码: %EXIT_CODE%
)

pause
exit /b %EXIT_CODE%
