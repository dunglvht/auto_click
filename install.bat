@echo off
setlocal
cd /d "%~dp0"
title Bit Heroes Bot - install
echo Looking for Python 3.9 or newer...
call "%~dp0scripts\find_python.bat"
if not defined PY call :get_python
if not defined PY (
  echo.
  echo Python was not found. Install it from the page that opens now
  echo ^(tick "Add python.exe to PATH"^), then double-click install.bat again.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)
echo Using %PY%
echo.
echo Installing what the bot needs (websocket-client, Pillow, numpy)...
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt pillow numpy
if errorlevel 1 (
  echo.
  echo Installing failed, see the messages above.
  pause
  exit /b 1
)

rem A desktop shortcut that starts the app without a console window.
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
set "APPDIR=%~dp0"
set ARGS="%~dp0autoclicker.py"
echo.
echo Creating the "Bit Heroes Bot" shortcut on the desktop...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Bit Heroes Bot.lnk'); $s.TargetPath = $env:PYW; $s.Arguments = $env:ARGS; $s.WorkingDirectory = $env:APPDIR; $s.Description = 'Bit Heroes Bot'; $s.Save()"
if errorlevel 1 echo Could not create the shortcut; use run.bat instead.

echo.
echo Done. Start the bot with the "Bit Heroes Bot" shortcut on your desktop,
echo or double-click run.bat in this folder.
pause
exit /b 0

:get_python
where winget >nul 2>nul || exit /b 0
echo Python is not installed. Installing Python 3.12 with winget, this can take a few minutes...
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call "%~dp0scripts\find_python.bat"
exit /b 0
