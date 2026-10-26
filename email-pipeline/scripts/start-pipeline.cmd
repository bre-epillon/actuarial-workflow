@echo off
rem Starts the email pipeline in this window. Close the window (or press Ctrl+C) to stop it.
rem To start it automatically at logon: Win+R, type shell:startup, and put a shortcut
rem to this file in the folder that opens.
cd /d "%~dp0.."
uv run python -m pipeline %*
echo.
echo The pipeline has stopped. Press any key to close this window.
pause >nul
