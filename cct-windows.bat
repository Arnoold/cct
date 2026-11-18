@echo off
setlocal
cd /d "%~dp0"
title Claude token switcher (cct)

rem ---- find Python 3 (skip the fake "python" that only opens Microsoft Store) ----
set PY=
py -3 -c "import sys" >nul 2>nul && set PY=py -3
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set PY=python
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set PY="%%D\python.exe"

if not defined PY (
  echo.
  echo Python 3 was not found on this computer.
  where winget >nul 2>nul
  if errorlevel 1 goto nopython
  choice /c YN /m "Install Python 3.12 now with winget"
  if errorlevel 2 goto nopython
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set PY="%%D\python.exe"
)
if not defined PY goto nopython

rem ---- install (only changes what is needed) and open the web page ----
%PY% cct.py setup
if errorlevel 1 pause
exit /b

:nopython
echo.
echo Please install Python 3 from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then double-click this file again.
pause
exit /b 1
