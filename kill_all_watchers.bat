@echo off
setlocal

echo [KILL] Scanning watcher processes... 

powershell -NoProfile -ExecutionPolicy Bypass -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python(\.exe)?' -and $_.CommandLine -match '--watch-input' }; if (-not $procs) { Write-Output '[KILL] No watcher process found.'; exit 0 }; $procs | ForEach-Object { Write-Output ('[KILL] Stopping PID=' + $_.ProcessId + ' CMD=' + $_.CommandLine); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Write-Output ('[KILL] Done. Stopped=' + $procs.Count)"

if errorlevel 1 (
  echo [KILL] Failed to kill watcher processes.
  exit /b 1
)

echo [KILL] Completed.
exit /b 0
