$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-PortListening {
  param([int]$Port)

  $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  return $null -ne $connections
}

Write-Host 'Starting Smart Badge frontend and backend...' -ForegroundColor Cyan

if (-not (Test-PortListening -Port 8000)) {
  Start-Process powershell -ArgumentList @(
    '-NoExit',
    '-ExecutionPolicy', 'Bypass',
    '-Command',
    "Set-Location '$root\apps\api'; `$env:PYTHONPATH='$root\apps\api\src'; & '$root\apps\api\.venv\Scripts\python.exe' -m uvicorn smart_badge_api.main:app --reload --host 0.0.0.0 --port 8000"
  )
  Write-Host '  API process started on port 8000' -ForegroundColor Green
} else {
  Write-Host '  API already listening on port 8000, skipping restart' -ForegroundColor Yellow
}

if (-not (Test-PortListening -Port 5173)) {
  Start-Process powershell -ArgumentList @(
    '-NoExit',
    '-ExecutionPolicy', 'Bypass',
    '-Command',
    "Set-Location '$root\apps\web'; pnpm run dev -- --host 0.0.0.0 --port 5173"
  )
  Write-Host '  Web process started on port 5173' -ForegroundColor Green
} else {
  Write-Host '  Web already listening on port 5173, skipping restart' -ForegroundColor Yellow
}

Write-Host ''
Write-Host 'Started:' -ForegroundColor Green
Write-Host '  Web: http://127.0.0.1:5173'
Write-Host '  API: http://127.0.0.1:8000/api/v1/docs'
Write-Host ''
Write-Host 'Keep any newly opened PowerShell windows running while using the system.' -ForegroundColor Yellow
