@echo off
setlocal

if not defined KEIRI_PYTHON (
    echo KEIRI_PYTHON environment variable is not set.
    echo Set it to your python.exe path, e.g.:
    echo   setx KEIRI_PYTHON "C:\keiri_python\python_embed\python.exe"
    pause
    exit /b 1
)
set PYTHON=%KEIRI_PYTHON%
set WORK_DIR=%~dp0

echo [1/3] Checking Nuitka...
%PYTHON% -m nuitka --version > nul 2>&1
if errorlevel 1 (
    echo Installing Nuitka...
    %PYTHON% -m pip install nuitka
    if errorlevel 1 ( echo FAILED & pause & exit /b 1 )
)
%PYTHON% -m nuitka --version
echo OK

echo.
echo [2/3] Building ArcHiveServer (Nuitka standalone) ...
echo NOTE: First run downloads MinGW64 and compiles to C. This takes 20-40min.
echo.
cd /d "%WORK_DIR%"

rem Clean old builds
if exist dist\ArcHiveServer       rmdir /s /q dist\ArcHiveServer
if exist dist\manga_server_app.dist rmdir /s /q dist\manga_server_app.dist

%PYTHON% -m nuitka ^
  --standalone ^
  --mingw64 ^
  --assume-yes-for-downloads ^
  --windows-disable-console ^
  --windows-icon-from-ico=assets\icon\app_icon.ico ^
  --output-dir=dist ^
  --output-filename=ArcHiveServer.exe ^
  --enable-plugin=tk-inter ^
  --include-data-dir=assets/icon=assets/icon ^
  --include-data-files=help.html=help.html ^
  --include-package=aiortc ^
  --include-package=aioice ^
  --include-package=av ^
  --include-package=pylibsrtp ^
  --include-package=google_crc32c ^
  --include-package=pyee ^
  --include-package=OpenSSL ^
  --include-package=cryptography ^
  --include-package=fitz ^
  --include-package=pymupdf ^
  --include-package=uvicorn ^
  --include-package=pystray ^
  --include-package=rarfile ^
  --include-package=PIL ^
  manga_server_app.py

if errorlevel 1 ( echo. & echo === Nuitka Build FAILED === & pause & exit /b 1 )

echo.
echo [3/3] Renaming output folder to ArcHiveServer...
if exist dist\manga_server_app.dist (
    rename dist\manga_server_app.dist ArcHiveServer
    echo Renamed: dist\manga_server_app.dist -> dist\ArcHiveServer
) else (
    echo WARNING: dist\manga_server_app.dist not found. Check output manually.
)

rem Copy UnRAR.exe if available
if exist "C:\Program Files\WinRAR\UnRAR.exe" (
    copy /y "C:\Program Files\WinRAR\UnRAR.exe" dist\ArcHiveServer\UnRAR.exe > nul
    echo UnRAR.exe copied.
)

echo.
echo ========================================================
echo Done: %WORK_DIR%dist\ArcHiveServer\ArcHiveServer.exe
echo Next : run build_archive_setup.bat to create installer
echo ========================================================
pause
endlocal
