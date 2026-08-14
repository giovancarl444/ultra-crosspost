@echo off
REM Double-click to start Crosspost Engine. Docker Desktop must be running.
cd /d "%~dp0.."

echo ============================================
echo   Crosspost Engine - starting
echo ============================================
echo.

docker version >nul 2>&1
if errorlevel 1 (
  echo Docker is not responding.
  echo Open Docker Desktop, wait until it says "Engine running", then run this again.
  echo.
  pause
  exit /b 1
)

if not exist ".env" (
  echo Missing .env  - copy .env.example to .env and fill it in first.
  echo.
  pause
  exit /b 1
)
if not exist "profiles.yaml" (
  echo Missing profiles.yaml  - copy profiles.example.yaml to profiles.yaml and fill it in first.
  echo.
  pause
  exit /b 1
)
if not exist "service-account.json" (
  echo Missing service-account.json  - Drive polling will be off until you add it.
  echo.
)

echo Building and starting ^(first run takes a few minutes^)...
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo Startup failed. Read the message above, or run logs.bat for detail.
  pause
  exit /b 1
)

echo.
echo Started. It keeps running in the background and restarts with Windows.
echo   logs.bat  - watch what it is doing
echo   stop.bat  - stop it
echo.
pause
