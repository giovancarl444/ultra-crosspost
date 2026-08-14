@echo off
REM Double-click to watch the log. Close the window or press Ctrl+C to stop watching -
REM that does NOT stop the engine.
cd /d "%~dp0.."
echo Showing live log. Closing this window does not stop the engine.
echo.
docker compose logs -f --tail 100
pause
