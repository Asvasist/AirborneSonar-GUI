@echo off
rem Airborne Sonar GUI launcher for Windows.
rem First run: creates a private Python environment in .venv and installs the
rem requirements into it. Later runs just start the application.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating the Python environment .venv ...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 goto nopython
)

".venv\Scripts\python.exe" -c "import tkinter" 2>nul
if errorlevel 1 goto notk

if not exist ".venv\.deps_ok" (
    echo Installing the requirements - one time, needs internet, a few minutes ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto pipfail
    echo ok> ".venv\.deps_ok"
)

".venv\Scripts\python.exe" "host\sonar_gui.py"
if errorlevel 1 pause
exit /b 0

:nopython
echo.
echo Python 3.9 or newer was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH", then run this file again.
rmdir /s /q .venv 2>nul
pause
exit /b 1

:notk
echo.
echo This Python has no Tkinter. Re-run the Python installer, choose Modify,
echo and enable "tcl/tk and IDLE", then run this file again.
rmdir /s /q .venv 2>nul
pause
exit /b 1

:pipfail
echo.
echo Installing the requirements failed. Check the internet connection and run this file again.
pause
exit /b 1
