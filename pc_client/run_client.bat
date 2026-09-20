@echo off
setlocal
set "SCRIPT=%~dp0archive_client.py"

rem Prefer pythonw so no console window shows up.
where pythonw.exe >NUL 2>NUL
if %errorlevel%==0 (
    start "" pythonw.exe "%SCRIPT%" %*
    exit /b
)
where pyw.exe >NUL 2>NUL
if %errorlevel%==0 (
    start "" pyw.exe -3 "%SCRIPT%" %*
    exit /b
)
echo Python 3 was not found on PATH.
echo Install Python 3 from https://www.python.org/ and try again.
pause
