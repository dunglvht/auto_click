@echo off
cd /d "%~dp0"
call "%~dp0scripts\find_python.bat"
if not defined PY (
  echo Python was not found. Double-click install.bat first.
  pause
  exit /b 1
)
rem Load the app once here first: without a console, a startup error would go unseen.
"%PY%" -c "import tab_autoclicker.gui"
if errorlevel 1 (
  echo.
  echo The app could not start, see the error above. If a package is missing,
  echo double-click install.bat first.
  pause
  exit /b 1
)
rem pythonw: the app's window only, no console window.
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" "%~dp0autoclicker.py"
