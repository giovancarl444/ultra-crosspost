@echo off
REM Double-click to stop Crosspost Engine. The queue database and media are kept.
cd /d "%~dp0.."
echo Stopping Crosspost Engine...
docker compose down
echo.
echo Stopped. Your queue and downloaded media are preserved in the data folder.
echo Run start.bat to bring it back.
echo.
pause
