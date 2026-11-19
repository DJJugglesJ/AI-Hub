@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_web_launcher.ps1" %*
exit /b %errorlevel%
