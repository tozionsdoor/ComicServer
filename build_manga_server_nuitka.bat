@echo off
setlocal

rem Nuitka build script for ArcHiveServer (Python 3.14 + ziglang)
set NUITKA_PY=C:\Users\taka\AppData\Local\Programs\Python\Python313\python.exe
set WORK_DIR=%~dp0

if not exist "%NUITKA_PY%" (
    echo ERROR: Python not found at %NUITKA_PY%
    pause & exit /b 1
)

echo [1/3] Nuitka version check...
"%NUITKA_PY%" -m nuitka --version --assume-yes-for-downloads
if errorlevel 1 (
    echo Installing Nuitka...
    "%NUITKA_PY%" -m pip install nuitka
    if errorlevel 1 ( echo FAILED & pause & exit /b 1 )
)
echo OK

echo.
echo [2/3] Building ArcHiveServer (Nuitka standalone) ...
echo NOTE: First run compiles everything to C. This takes 20-40 min.
echo.
cd /d "%WORK_DIR%"

rem Clean old builds
if exist dist\ArcHiveServer         rmdir /s /q dist\ArcHiveServer
if exist dist\manga_server_app.dist rmdir /s /q dist\manga_server_app.dist

"%NUITKA_PY%" -m nuitka ^
  --standalone ^
  --assume-yes-for-downloads ^
  --windows-console-mode=disable ^
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
  --include-package=aiofiles ^
  --include-package=fastapi ^
  --include-package=pydantic ^
  --include-package=starlette ^
  --include-package=anyio ^
  manga_server_app.py

if errorlevel 1 ( echo. & echo === Nuitka Build FAILED === & pause & exit /b 1 )

echo.
echo [3/3] Renaming output folder to ArcHiveServer...
if exist dist\manga_server_app.dist (
    rename dist\manga_server_app.dist ArcHiveServer
    echo Renamed OK
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
