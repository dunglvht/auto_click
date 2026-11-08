@echo off
rem Sets PY to a python.exe that is 3.9 or newer and has Tk, or leaves PY empty.
rem Skips the Microsoft Store stand-in (WindowsApps\python.exe), which only opens the Store.
set "PY="
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do call :try "%%P"
if defined PY exit /b 0
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do call :try "%%~D\python.exe"
if defined PY exit /b 0
for /f "delims=" %%P in ('where python 2^>nul') do call :try "%%P"
exit /b 0

:try
if defined PY exit /b 0
if not exist "%~1" exit /b 0
echo %~1| find /i "\WindowsApps\" >nul && exit /b 0
"%~1" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul || exit /b 0
set "PY=%~1"
exit /b 0
