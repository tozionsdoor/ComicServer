@echo off
rem Starts the review (Play Store demo) instance of ArcHiveServer: sample books
rem only, own HTTP port and own IPC port, so it runs next to the personal
rem ArcHiveServer.exe. It is NOT started at logon any more (since 2026-11-23):
rem start it by hand only while an app update is under Play review, via the
rem shortcut review_server_*.lnk. Do not start it from a Claude shell, or it
rem stops when the Claude app updates or quits.
rem
rem KEEP THIS FILE ASCII-ONLY. cmd reads batch files as CP932; UTF-8 Japanese
rem comments shift the line positions and commands get skipped silently (the
rem KEIRI_PYTHON reset below was lost that way and the server died at startup).

set "ARCHIVE_APP_DIR=Z:\Taka_Documents\ComicServer\review_server"
set "ARCHIVE_IPC_PORT=18766"
set "ARCHIVE_AUTOSTART=1"
rem KEIRI_PYTHON injects the accounting module's site-packages. This server does
rem not need it, and its PIL build has a different ABI and crashes on import.
set "KEIRI_PYTHON="

set "PYW=C:\Users\taka\AppData\Local\Programs\Python\Python313\pythonw.exe"
set "SCRIPT=Z:\Taka_Documents\ComicServer\manga_server_app.py"

start "" "%PYW%" "%SCRIPT%"
