@echo off
REM Starts the A.C.E.S. server on this PC and opens the chat in your browser.
REM Double-click to run. To re-enter the saved secrets: start-aces.bat reset
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-aces.ps1" %*
if errorlevel 1 pause
