@echo off
REM Double-click to validate the configuration without starting anything.
REM Prints the profiles and lists any missing credentials. Never prints a secret.
cd /d "%~dp0.."
docker compose run --rm --no-deps crosspost python -m app --check-config
echo.
pause
