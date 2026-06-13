# -*- mode: python ; coding: utf-8 -*-
# archive_setup.spec - ArcHive Server インストーラー（PyInstaller, onefile）
# 事前に build_manga_server.bat で dist\ArcHiveServer を作成しておくこと
# 実行: build_archive_setup.bat -> dist\ArcHive_Setup.exe

a = Analysis(
    ['archive_setup.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['win32com.client'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# ArcHiveServer本体（onedirビルド）を丸ごと同梱
a.datas += Tree('dist/ArcHiveServer', prefix='ArcHiveServer')

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ArcHive_Setup',
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
