@echo off
REM Starts the A.C.E.S. server on this PC and opens the chat in your browser.
REM Double-click to run against the county database (ASRCustomerService on asbcdbbernt).
REM Already running? Double-click again to restart it.
REM Options, from a command prompt in this folder (they combine):
REM   start-aces.bat azure      use the Azure ACES_Test database instead
REM   start-aces.bat shortcut   also put an "A.C.E.S. Server" shortcut on your desktop
REM   start-aces.bat reset      re-enter the saved secrets
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-aces.ps1" %*
if errorlevel 1 pause
