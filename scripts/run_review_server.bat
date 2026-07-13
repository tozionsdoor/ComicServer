@echo off
rem 審査用ArcHiveServerインスタンス（サンプル本のみ・専用ポート/専用IPC）を起動する。
rem 個人利用のArcHiveServer.exeとは完全に独立して同時起動できる。
rem Windowsスタートアップフォルダにこのバッチのショートカットを置けば、
rem ログオン時に自動でこのインスタンスが立ち上がる。

set "ARCHIVE_APP_DIR=Z:\Taka_Documents\ComicServer\review_server"
set "ARCHIVE_IPC_PORT=18766"
set "ARCHIVE_AUTOSTART=1"
rem KEIRI_PYTHON経由のsite-packages挿入は経理モジュール専用でこのサーバーには不要かつPILのABI不一致を起こすため無効化
set "KEIRI_PYTHON="

set "PYW=C:\Users\taka\AppData\Local\Programs\Python\Python313\pythonw.exe"
set "SCRIPT=Z:\Taka_Documents\ComicServer\manga_server_app.py"

start "" "%PYW%" "%SCRIPT%"
