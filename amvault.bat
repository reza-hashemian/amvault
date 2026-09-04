@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
title amvault

cd /d "%~dp0"

REM One-click launcher for Windows. Sets up a private Python environment on the
REM first run, then starts the app. Every later run skips straight to launching.
REM
REM Written defensively because the person double-clicking this may not know
REM what Python is: each failure explains what to do rather than showing a
REM traceback, and the window is kept open so the message can be read.

set "VENV=.venv-win"
set "PYEXE=%VENV%\Scripts\python.exe"

echo.
echo   amvault
echo   ==========================================
echo.

REM ---------- already installed? ----------
REM The interpreter existing is not proof the setup finished: `venv` creates it
REM before pip installs anything, so a download interrupted half way leaves a
REM working python.exe with no dependencies behind it. Test a real import.
if not exist "%PYEXE%" goto :setup
"%PYEXE%" -c "import PySide6, pykeepass" >nul 2>&1
if not errorlevel 1 goto :launch
echo   The last setup did not finish. Resuming where it stopped...
echo.
goto :install

:setup

REM ---------- find a usable Python ----------
echo   First run - setting things up. This takes a few minutes.
echo.

set "PYCMD="

REM The py launcher ships with python.org installers and is the most reliable
REM way to find a real Python, so try it before a bare `python`.
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYCMD=py -3"
    goto :found
)

REM A bare `python` on a machine without Python opens the Microsoft Store stub,
REM which prints nothing and exits 0 instead of failing. So require that it
REM actually reports a 3.x version.
REM
REM Each test is its own line rather than a nested if() block: cmd.exe expands
REM %VAR% across a whole block before running it, so a variable assigned inside
REM the block would still read as empty when tested.
set "PYVER="
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
if not defined PYVER goto :nopython
echo !PYVER! | findstr /r "^3\." >nul
if errorlevel 1 goto :nopython
REM 3.0-3.9 satisfy the pattern above but are too old for the dependencies;
REM rejecting them here gives a clear message instead of a pip resolution error.
echo !PYVER! | findstr /r "^3\.[0-9]\." >nul
if not errorlevel 1 goto :tooold
set "PYCMD=python"
goto :found

:tooold
echo   [X] Python !PYVER! is too old.
echo.
echo   amvault needs Python 3.10 or newer.
echo   Install a current version from https://www.python.org/downloads/
echo   and remember to tick "Add python.exe to PATH".
echo.
start "" "https://www.python.org/downloads/"
pause
exit /b 1

:nopython

REM ---------- no Python ----------
echo   [X] Python is not installed on this computer.
echo.
echo   amvault needs Python 3.10 or newer to run.
echo.
echo   How to install it:
echo.
echo     1. Go to      https://www.python.org/downloads/
echo     2. Click the big yellow "Download Python" button
echo     3. Run the installer
echo     4. IMPORTANT: tick "Add python.exe to PATH" on the first screen
echo     5. Click "Install Now"
echo     6. When it finishes, double-click this file again
echo.
echo   Opening the download page for you...
timeout /t 3 >nul
start "" "https://www.python.org/downloads/"
echo.
pause
exit /b 1

:found
for /f "tokens=2" %%v in ('%PYCMD% --version 2^>^&1') do set "FOUNDVER=%%v"
echo   [OK] Found Python %FOUNDVER%
echo.

REM ---------- create the environment ----------
echo   Creating a private Python environment...
if exist "%PYEXE%" goto :venvready
%PYCMD% -m venv "%VENV%" 2>nul
if not exist "%PYEXE%" (
    echo.
    echo   [X] Could not create the environment.
    echo.
    echo   This usually means Python was installed without the "venv" module,
    echo   or this folder is read-only.
    echo.
    echo   Try: reinstall Python from python.org, or move this folder
    echo        somewhere you can write to, such as your Desktop.
    echo.
    pause
    exit /b 1
)
:venvready
echo   [OK] Environment ready
echo.

:install
REM ---------- install dependencies ----------
echo   Downloading the parts amvault needs.
echo   This is the slow step - about 250 MB. Please wait.
echo.

"%PYEXE%" -m pip install --upgrade pip --quiet --disable-pip-version-check 2>nul
"%PYEXE%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo   [X] Download failed.
    echo.
    echo   Check that this computer is connected to the internet, then
    echo   double-click this file again. It carries on from where it stopped -
    echo   parts already downloaded are not fetched twice.
    echo.
    pause
    exit /b 1
)

echo.
echo   [OK] Everything installed
echo.

REM ---------- check for ssh (optional) ----------
where ssh >nul 2>&1
if errorlevel 1 (
    echo   Note: OpenSSH was not found. Passwords and 2FA codes work fine
    echo         without it, but connecting to servers from inside amvault
    echo         will not. To add it later:
    echo         Settings ^> Apps ^> Optional Features ^> OpenSSH Client
    echo.
)

echo   Setup complete. Starting amvault...
echo.
timeout /t 2 >nul

:launch
REM Check the app can import before handing off to pythonw.exe: that binary has
REM no console, so a failure after this point dies invisibly and looks to the
REM user like clicking the file did nothing at all.
"%PYEXE%" -c "import PySide6, pykeepass, argon2, cryptography" >nul 2>&1
if not errorlevel 1 goto :run

echo   [X] Some parts are missing or damaged. Repairing...
echo.
"%PYEXE%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :repairfailed
echo.
echo   [OK] Repaired.
echo.
goto :run

:repairfailed
echo.
echo   [X] Could not repair the installation.
echo.
echo   Delete the folder called  %VENV%  in this directory, then
echo   double-click this file again to start over from scratch.
echo.
pause
exit /b 1

:run
REM pythonw.exe runs without a console window, which is what you want for a
REM desktop app. Fall back to python.exe if it is missing.
set "PYW=%VENV%\Scripts\pythonw.exe"
if not exist "%PYW%" goto :runconsole
start "" "%PYW%" "amvault-gui.py"
exit /b 0

:runconsole

"%PYEXE%" "amvault-gui.py"
if errorlevel 1 (
    echo.
    echo   [X] amvault could not start.
    echo.
    echo   To reset and try again, delete the "%VENV%" folder in this
    echo   directory and double-click this file once more.
    echo.
    pause
    exit /b 1
)

exit /b 0
