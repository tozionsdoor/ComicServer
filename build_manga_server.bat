@echo off
setlocal

set PYTHON=C:\keiri_python\python_embed\python.exe
set WORK_DIR=%~dp0

echo [1/2] Checking PyInstaller...
%PYTHON% -m PyInstaller --version > nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    %PYTHON% -m pip install pyinstaller
    if errorlevel 1 ( echo FAILED & pause & exit /b 1 )
)
echo OK

echo [2/2] Building ArcHiveServer.exe...
cd /d "%WORK_DIR%"
%PYTHON% -m PyInstaller manga_server_app.spec --noconfirm
if errorlevel 1 ( echo Build FAILED & pause & exit /b 1 )

echo.
echo Done: %WORK_DIR%dist\ArcHiveServer\ArcHiveServer.exe
pause
endlocal
