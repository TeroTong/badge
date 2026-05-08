@echo off
setlocal

set ROOT=%~dp0

echo Starting Smart Badge frontend and backend...

set API_RUNNING=
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { 'yes' }"') do set API_RUNNING=%%i

if /I not "%API_RUNNING%"=="yes" (
	start "Smart Badge API" powershell -NoExit -ExecutionPolicy Bypass -Command "Set-Location '%ROOT%apps\api'; $env:PYTHONPATH='%ROOT%apps\api\src'; & '%ROOT%apps\api\.venv\Scripts\python.exe' -m uvicorn smart_badge_api.main:app --reload --host 0.0.0.0 --port 8000"
	echo API process started on port 8000.
) else (
	echo API already listening on port 8000, skipping restart.
)

set WEB_RUNNING=
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue) { 'yes' }"') do set WEB_RUNNING=%%i

if /I not "%WEB_RUNNING%"=="yes" (
	start "Smart Badge Web" powershell -NoExit -ExecutionPolicy Bypass -Command "Set-Location '%ROOT%apps\web'; pnpm run dev -- --host 0.0.0.0 --port 5173"
	echo Web process started on port 5173.
) else (
	echo Web already listening on port 5173, skipping restart.
)

echo.
echo API window and Web window have been started.
echo Web: http://127.0.0.1:5173
echo API: http://127.0.0.1:8000/api/v1/docs
echo.
pause
