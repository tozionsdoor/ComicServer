@echo off
setlocal enabledelayedexpansion
chcp 65001 > nul

echo =====================================================
echo  ComicServer Android 開発環境セットアップ
echo =====================================================
echo.

set FLUTTER_ZIP=C:\Users\taka\Downloads\flutter_sdk.zip
set FLUTTER_DIR=C:\Users\taka\flutter
set ANDROID_DIR=C:\Users\taka\android-sdk
set CMDTOOLS_ZIP=C:\Users\taka\Downloads\cmdtools.zip
set PATH_ENTRY=%FLUTTER_DIR%\bin

:: ── Flutter SDK 展開 ──────────────────────────────────────
echo [1/4] Flutter SDK を展開中...
if not exist "%FLUTTER_ZIP%" (
    echo エラー: %FLUTTER_ZIP% が見つかりません。
    pause & exit /b 1
)
if exist "%FLUTTER_DIR%" (
    echo Flutter は既に展開済みです。スキップします。
) else (
    powershell -Command "Expand-Archive -Path '%FLUTTER_ZIP%' -DestinationPath 'C:\Users\taka' -Force"
    if errorlevel 1 ( echo 展開失敗 & pause & exit /b 1 )
    echo 完了
)

:: ── PATH 追加（現在のユーザー） ───────────────────────────
echo [2/4] PATH に Flutter を追加中...
powershell -Command "$p=[Environment]::GetEnvironmentVariable('PATH','User'); if($p -notlike '*%FLUTTER_DIR%\bin*'){[Environment]::SetEnvironmentVariable('PATH',$p+';%FLUTTER_DIR%\bin','User'); Write-Host '追加完了'} else {Write-Host '既に設定済み'}"
set PATH=%PATH%;%FLUTTER_DIR%\bin

:: ── Android Command Line Tools ────────────────────────────
echo [3/4] Android コマンドラインツールをダウンロード中...
if not exist "%CMDTOOLS_ZIP%" (
    powershell -Command "Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/commandlinetools-win-13114758_latest.zip' -OutFile '%CMDTOOLS_ZIP%' -UseBasicParsing"
    if errorlevel 1 ( echo ダウンロード失敗 & pause & exit /b 1 )
)
if not exist "%ANDROID_DIR%\cmdline-tools\latest\bin\sdkmanager.bat" (
    powershell -Command "Expand-Archive -Path '%CMDTOOLS_ZIP%' -DestinationPath '%ANDROID_DIR%\cmdline-tools\latest' -Force"
    :: cmdline-tools の中身を latest に移動
    powershell -Command "Get-ChildItem '%ANDROID_DIR%\cmdline-tools\latest\cmdline-tools' | Move-Item -Destination '%ANDROID_DIR%\cmdline-tools\latest\' -Force; Remove-Item '%ANDROID_DIR%\cmdline-tools\latest\cmdline-tools' -Recurse -Force" 2>nul
    echo 展開完了
)

:: ── Android SDK コンポーネント ──────────────────────────────
echo [4/4] Android SDK をインストール中（少し時間がかかります）...
set ANDROID_HOME=%ANDROID_DIR%
set PATH=%PATH%;%ANDROID_DIR%\cmdline-tools\latest\bin;%ANDROID_DIR%\platform-tools

echo y | "%ANDROID_DIR%\cmdline-tools\latest\bin\sdkmanager.bat" "platform-tools" "platforms;android-35" "build-tools;35.0.0"
if errorlevel 1 ( echo SDK インストール失敗 & pause & exit /b 1 )

:: ── flutter doctor ─────────────────────────────────────────
echo.
echo ── Flutter Doctor ──────────────────────────────────────
"%FLUTTER_DIR%\bin\flutter.bat" config --android-sdk "%ANDROID_DIR%"
"%FLUTTER_DIR%\bin\flutter.bat" doctor --android-licenses
"%FLUTTER_DIR%\bin\flutter.bat" doctor

echo.
echo =====================================================
echo  セットアップ完了！
echo  次のステップ：
echo    1. Android スマートフォンでUSBデバッグを有効化
echo    2. USBで接続
echo    3. build_apk.bat を実行
echo =====================================================
pause
