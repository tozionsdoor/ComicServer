@echo off
setlocal

set ISCC="C:\Program Files (x86)\Inno Setup 7\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 7\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"

if not exist %ISCC% (
    echo ERROR: Inno Setup 6 not found.
    echo Please install from https://jrsoftware.org/isdl.php
    pause & exit /b 1
)

set WORK_DIR=%~dp0
cd /d "%WORK_DIR%"

if not exist dist\ArcHiveServer\ArcHiveServer.exe (
    echo ERROR: dist\ArcHiveServer\ArcHiveServer.exe not found.
    echo Run build_manga_server_nuitka.bat first.
    pause & exit /b 1
)

echo Building ArcHive_Setup.exe with Inno Setup...
%ISCC% archive_setup.iss
if errorlevel 1 ( echo Build FAILED & pause & exit /b 1 )

echo.
echo Done: %WORK_DIR%dist\ArcHive_Setup.exe
pause
endlocal
