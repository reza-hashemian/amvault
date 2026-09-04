@echo off
chcp 65001 >nul 2>&1
title amvault - reset
cd /d "%~dp0"

REM Deletes the Python environment so the next launch rebuilds it from scratch.
REM The vault in data\ is never touched -- this only removes downloaded
REM libraries, which can always be fetched again.

echo.
echo   amvault - reset the installation
echo   ==========================================
echo.
echo   This deletes the downloaded Python libraries so the next
echo   launch installs them fresh.
echo.
echo   Your passwords in  data\vault.kdbx  are NOT touched.
echo.

if not exist ".venv-win" (
    echo   Nothing to reset - no installation found.
    echo.
    echo   Just double-click amvault.bat to install.
    echo.
    pause
    exit /b 0
)

set /p ANSWER="  Type Y and press Enter to continue: "
if /i not "%ANSWER%"=="Y" (
    echo.
    echo   Cancelled. Nothing was changed.
    echo.
    pause
    exit /b 0
)

echo.
echo   Removing...
rmdir /s /q ".venv-win"

if exist ".venv-win" (
    echo.
    echo   [X] Could not remove the folder.
    echo.
    echo   Close amvault if it is running, then try again. If it still
    echo   fails, delete the folder named  .venv-win  by hand.
    echo.
    pause
    exit /b 1
)

echo   [OK] Removed.
echo.
echo   Now double-click  amvault.bat  to install again.
echo.
pause
exit /b 0
