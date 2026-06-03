@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

set FLUTTER=%USERPROFILE%\flutter\bin\flutter.bat
set ANDROID_HOME=%USERPROFILE%\android-sdk
set APP_DIR=%~dp0comicserver_app

echo =====================================================
echo  ComicServer APK ビルド
echo =====================================================
echo.

:: android/ フォルダがない場合は flutter create で生成
if not exist "%APP_DIR%\android\" (
    echo [0/4] Flutter プロジェクト初期化中...
    set TMPDIR=%TEMP%\cs_tmp_%RANDOM%
    "%FLUTTER%" create --org com.comicserver --project-name comicserver_app "!TMPDIR!"
    if errorlevel 1 ( echo flutter create 失敗 & pause & exit /b 1 )

    :: android/ ios/ windows/ など必要なフォルダをコピー
    xcopy /E /I /Y "!TMPDIR!\android"  "%APP_DIR%\android\"
    xcopy /E /I /Y "!TMPDIR!\ios"      "%APP_DIR%\ios\"

    :: AndroidManifest は上書き（INTERNET 許可を含む我々のものを使う）
    copy /Y "%APP_DIR%\android\app\src\main\AndroidManifest.xml" "%APP_DIR%\android\app\src\main\AndroidManifest.xml" > nul 2>&1

    rmdir /S /Q "!TMPDIR!"
    echo 初期化完了
)

:: pub get
echo [1/3] パッケージ取得中...
cd /d "%APP_DIR%"
"%FLUTTER%" pub get
if errorlevel 1 ( echo pub get 失敗 & pause & exit /b 1 )

:: デバイス確認
echo.
echo 接続済みデバイス:
"%FLUTTER%" devices
echo.

:: APK ビルド
echo [2/3] APK ビルド中（初回は数分かかります）...
"%FLUTTER%" build apk --release
if errorlevel 1 ( echo ビルド失敗 & pause & exit /b 1 )

echo [3/3] 完了！
echo.
echo APK の場所:
echo   %APP_DIR%\build\app\outputs\flutter-apk\app-release.apk
echo.
echo スマホにインストールする場合:
echo   1. USB デバッグで接続
echo   2. 下記コマンドを実行:
echo      %USERPROFILE%\android-sdk\platform-tools\adb.exe install "%APP_DIR%\build\app\outputs\flutter-apk\app-release.apk"
echo.
pause
