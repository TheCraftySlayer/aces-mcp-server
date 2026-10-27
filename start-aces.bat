@echo off
REM Starts the A.C.E.S. server on this PC and opens the chat in your browser.
REM Double-click to run (Azure ACES_Test database).
REM County database instead: start-aces.bat onprem
REM Re-enter the saved secrets: add reset, e.g. start-aces.bat onprem reset
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-aces.ps1" %*
if errorlevel 1 pause
