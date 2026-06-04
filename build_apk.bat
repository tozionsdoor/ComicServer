@echo off
setlocal

set FLUTTER=%USERPROFILE%\flutter\bin\flutter.bat
set APP_DIR=%~dp0comicserver_app

echo ===================================
echo  ComicServer APK Build
echo ===================================
echo.

echo [1/3] pub get...
cd /d "%APP_DIR%"
"%FLUTTER%" pub get
if errorlevel 1 ( echo pub get failed ^& pause ^& exit /b 1 )

echo [2/3] Building APK...
"%FLUTTER%" build apk --release
if errorlevel 1 ( echo Build failed ^& pause ^& exit /b 1 )

echo [3/3] Copying APK to project root...
copy /Y "%APP_DIR%\build\app\outputs\flutter-apk\app-release.apk" "%~dp0app-release.apk"
if errorlevel 1 ( echo Copy failed ^& pause ^& exit /b 1 )

echo Done!
echo.
echo APK: %~dp0app-release.apk
echo.
pause