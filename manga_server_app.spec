# -*- mode: python ; coding: utf-8 -*-
# manga_server_app.spec - ArcHive サーバー本体（PyInstaller, onedir）
# 実行: build_manga_server.bat -> dist\ArcHiveServer\ArcHiveServer.exe

import os
from PyInstaller.utils.hooks import collect_all

datas = [
    ('assets/icon', 'assets/icon'),
]
binaries = []
hiddenimports = []

# aiortc (WebRTC) とその依存、PDF/EPUB(pymupdf)、トレイ常駐(pystray)、
# RAR(rarfile)、サーバー(uvicorn) はフックが無い/不足しているため
# collect_all で datas/binaries/hiddenimports を丸ごと取り込む。
for pkg in (
    'aiortc', 'aioice', 'av', 'pylibsrtp', 'google_crc32c', 'pyee', 'OpenSSL',
    'cryptography', 'pymupdf', 'uvicorn', 'pystray', 'rarfile',
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# UnRAR.exe を同梱（配布先PCにWinRARが無くてもCBR/RARを開けるように）
_unrar = r"C:\Program Files\WinRAR\UnRAR.exe"
if os.path.exists(_unrar):
    binaries.append((_unrar, '.'))

a = Analysis(
    ['manga_server_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ArcHiveServer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon/app_icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ArcHiveServer',
)
