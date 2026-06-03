@echo off
setlocal

set PYTHON=C:\keiri_python\python_embed\python.exe
set WORK_DIR=%~dp0

echo [1/2] PyInstaller の確認...
%PYTHON% -m PyInstaller --version > nul 2>&1
if errorlevel 1 (
    echo PyInstaller をインストール中...
    %PYTHON% -m pip install pyinstaller
    if errorlevel 1 ( echo FAILED & pause & exit /b 1 )
)
echo OK

echo [2/2] MangaServer_setup.exe をビルド中...
cd /d "%WORK_DIR%"
%PYTHON% -m PyInstaller manga_server_setup.spec --noconfirm
if errorlevel 1 ( echo Build FAILED & pause & exit /b 1 )

echo.
echo 完成: %WORK_DIR%dist\MangaServer_setup.exe
pause
endlocal
