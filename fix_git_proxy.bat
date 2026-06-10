@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PAC_URL=http://rbins-ap.bosch.com/sgp.pac"
set "FALLBACK_PROXY=http://rb-proxy-apac.bosch.com:8080"
set "SECONDARY_FALLBACK_PROXY=http://rb-proxy-de.bosch.com:8080"
set "PROXY="
set "MODE=%~1"
if "%MODE%"=="" set "MODE=on"

echo.
echo ============================================================
echo  Git network helper
echo  Mode: %MODE%
echo ============================================================
echo.

if /I "%MODE%"=="status" goto :status
if /I "%MODE%"=="test" goto :test
if /I "%MODE%"=="off" goto :disable
if /I "%MODE%"=="on" goto :enable
if /I "%MODE%"=="pac" goto :pac_auto

echo [ERROR] Unknown mode: %MODE%
echo [INFO] Usage:
echo        fix_git_proxy.bat           ^(enable + test^)
echo        fix_git_proxy.bat on
echo        fix_git_proxy.bat on http://proxy-host:port
echo        fix_git_proxy.bat pac
echo        fix_git_proxy.bat off
echo        fix_git_proxy.bat status
echo        fix_git_proxy.bat test
exit /b 1

:enable
if not "%~2"=="" (
  set "PROXY=%~2"
  echo [INFO] Using manual proxy from argument: !PROXY!
  goto :apply_proxy
)

echo [INFO] Auto-discovering proxy from PAC: %PAC_URL%
goto :pac_auto

:pac_auto
set "PAC_TMP=%TEMP%\git_proxy_candidates_%RANDOM%%RANDOM%.txt"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$u='%PAC_URL%'; try { $raw=(Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 20).Content } catch { exit 2 }; if($raw -is [byte[]]){ $pac=[System.Text.Encoding]::UTF8.GetString($raw) } else { $pac=[string]$raw }; $set=New-Object 'System.Collections.Generic.HashSet[string]'; $varMatches=[regex]::Matches($pac,'var\s+\w*Proxy\s*=\s*([^;]+);',[System.Text.RegularExpressions.RegexOptions]::IgnoreCase); foreach($m in $varMatches){ $v=$m.Groups[1].Value.Trim().Trim('"',''''); if($v -match '^[A-Za-z0-9\.-]+:\d+$'){ $null=$set.Add($v) } }; $proxyMatches=[regex]::Matches($pac,'PROXY\s+([A-Za-z0-9\.-]+:\d+)'); foreach($m in $proxyMatches){ $null=$set.Add($m.Groups[1].Value) }; if($set.Count -eq 0){ exit 3 }; $set" > "%PAC_TMP%"
if errorlevel 1 (
  echo [WARN] PAC parse failed. Trying fallback proxies.
  set "TRY_PROXY=%FALLBACK_PROXY%"
  call :test_proxy_candidate
  if defined FOUND_PROXY goto :use_found_proxy
  set "TRY_PROXY=%SECONDARY_FALLBACK_PROXY%"
  call :test_proxy_candidate
  if defined FOUND_PROXY goto :use_found_proxy
  set "PROXY=%FALLBACK_PROXY%"
  goto :apply_proxy
)

set "FOUND_PROXY="
for /f "usebackq delims=" %%i in ("%PAC_TMP%") do (
  set "TRY_PROXY=%%i"
  call :test_proxy_candidate
  if defined FOUND_PROXY goto :proxy_found
)

:proxy_found
if exist "%PAC_TMP%" del /q "%PAC_TMP%" >nul 2>&1

:use_found_proxy
if defined FOUND_PROXY set "PROXY=%FOUND_PROXY%"
if defined PROXY (
  echo [SUCCESS] Selected proxy: !PROXY!
  goto :apply_proxy
)

echo [WARN] No PAC proxy candidate passed test. Falling back to primary proxy: %FALLBACK_PROXY%
set "PROXY=%FALLBACK_PROXY%"
goto :apply_proxy

:apply_proxy
echo [INFO] Setting global git proxy to: !PROXY!
git config --global http.proxy "!PROXY!"
if errorlevel 1 goto :git_fail
git config --global https.proxy "!PROXY!"
if errorlevel 1 goto :git_fail

if exist ".git" (
  rem Avoid local repo proxy override conflicts.
  git config --local --unset-all http.proxy >nul 2>&1
  git config --local --unset-all https.proxy >nul 2>&1
)

echo [SUCCESS] Global proxy updated.
goto :test

:disable
echo [INFO] Removing global git proxy settings...
git config --global --unset-all http.proxy >nul 2>&1
git config --global --unset-all https.proxy >nul 2>&1
echo [SUCCESS] Global proxy removed.
goto :status

:status
set "HTTP_PROXY_VAL="
set "HTTPS_PROXY_VAL="
for /f "usebackq delims=" %%i in (`git config --global --get http.proxy 2^>nul`) do set "HTTP_PROXY_VAL=%%i"
for /f "usebackq delims=" %%i in (`git config --global --get https.proxy 2^>nul`) do set "HTTPS_PROXY_VAL=%%i"

echo [INFO] Global http.proxy : !HTTP_PROXY_VAL!
echo [INFO] Global https.proxy: !HTTPS_PROXY_VAL!
if not defined HTTP_PROXY_VAL echo [WARN] http.proxy not set.
if not defined HTTPS_PROXY_VAL echo [WARN] https.proxy not set.

if exist ".git" (
  set "LOCAL_HTTP_PROXY_VAL="
  set "LOCAL_HTTPS_PROXY_VAL="
  for /f "usebackq delims=" %%i in (`git config --local --get http.proxy 2^>nul`) do set "LOCAL_HTTP_PROXY_VAL=%%i"
  for /f "usebackq delims=" %%i in (`git config --local --get https.proxy 2^>nul`) do set "LOCAL_HTTPS_PROXY_VAL=%%i"
  if defined LOCAL_HTTP_PROXY_VAL echo [WARN] Local http.proxy override: !LOCAL_HTTP_PROXY_VAL!
  if defined LOCAL_HTTPS_PROXY_VAL echo [WARN] Local https.proxy override: !LOCAL_HTTPS_PROXY_VAL!
)

echo.
echo [INFO] Tip: run "fix_git_proxy.bat test" to verify connectivity.
exit /b 0

:test
echo.
echo [INFO] Testing DNS: github.com
nslookup github.com >nul 2>&1
if errorlevel 1 (
  echo [WARN] DNS lookup failed for github.com in current network.
) else (
  echo [SUCCESS] DNS lookup works.
)

echo.
echo [INFO] Testing git remote access via public repo...
call :probe_git_with_retry public
if errorlevel 1 (
  echo [FAILED] git ls-remote test failed after retries.
  echo [INFO] This is usually a temporary corporate proxy outage (HTTP 503) or VPN path issue.
  exit /b 2
)

echo [SUCCESS] GitHub reachability test passed.

if exist ".git" (
  for /f "usebackq delims=" %%i in (`git remote get-url origin 2^>nul`) do set "ORIGIN_URL=%%i"
  if defined ORIGIN_URL (
    echo.
    echo [INFO] Testing current repo origin: !ORIGIN_URL!
    call :probe_git_with_retry origin
    if errorlevel 1 (
      echo [WARN] Origin test failed. Public GitHub is reachable, but origin may require auth or is temporarily unavailable.
      exit /b 3
    )

    echo [SUCCESS] Origin is reachable.
  )
)

exit /b 0

:probe_git_with_retry
set "TARGET=%~1"
set /a ATTEMPT=1

:probe_retry_loop
if /I "%TARGET%"=="origin" (
  git -c credential.interactive=never -c http.lowSpeedLimit=1 -c http.lowSpeedTime=20 -c http.connectTimeout=15 ls-remote origin HEAD >nul 2>&1
) else (
  git -c credential.interactive=never -c http.lowSpeedLimit=1 -c http.lowSpeedTime=20 -c http.connectTimeout=15 ls-remote https://github.com/git/git HEAD >nul 2>&1
)

if not errorlevel 1 exit /b 0
if %ATTEMPT% GEQ 3 exit /b 1

if /I "%TARGET%"=="origin" (
  echo [WARN] Origin test attempt %ATTEMPT% failed. Retrying...
) else (
  echo [WARN] Public repo test attempt %ATTEMPT% failed. Retrying...
)

set /a ATTEMPT+=1
goto :probe_retry_loop

:test_proxy_candidate
set "CAND=!TRY_PROXY!"
if /I "!CAND:~0,7!" NEQ "http://" if /I "!CAND:~0,8!" NEQ "https://" set "CAND=http://!CAND!"
echo [INFO] Testing proxy candidate: !CAND!
git -c http.proxy=!CAND! -c https.proxy=!CAND! -c credential.interactive=never -c http.lowSpeedLimit=1 -c http.lowSpeedTime=20 -c http.connectTimeout=15 ls-remote https://github.com/git/git HEAD >nul 2>&1
if not errorlevel 1 set "FOUND_PROXY=!CAND!"
exit /b 0

:git_fail
echo [FAILED] Failed to update git config. Is git available in PATH?
exit /b 1
