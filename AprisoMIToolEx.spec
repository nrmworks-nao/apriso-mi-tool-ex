# -*- mode: python ; coding: utf-8 -*-
# AprisoMIToolEx の PyInstaller ビルド設定
#
# ビルドは build.bat から `pyinstaller --noconfirm AprisoMIToolEx.spec` で実行する
# （CLI オプションで再生成すると本ファイルのカスタマイズが上書きされるため、spec 経由でビルドする）。
#
# PySide6 のフックは pyinstaller-hooks-contrib が自動処理する。
# pyodbc（拡張モジュール）と keyring の Windows バックエンドは動的に読み込まれるため、
# 同梱漏れを防ぐよう hiddenimports に明示する。

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'pyodbc',
        'keyring',
        'keyring.backends',
        'keyring.backends.Windows',
        'win32ctypes',
        'win32ctypes.core',
        'openpyxl',
        'openpyxl.cell._writer',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 使用しない標準 GUI ライブラリを除外して配布サイズを抑える
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AprisoMIToolEx',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI アプリのためコンソールウィンドウは表示しない
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# フォルダ配布形式（onedir）: dist\AprisoMIToolEx\ に exe と依存ファイルを展開する。
# config.json / backup\ / log\ は exe と同じフォルダに実行時に生成される。
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AprisoMIToolEx',
)
