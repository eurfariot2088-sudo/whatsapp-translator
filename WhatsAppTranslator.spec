# -*- mode: python ; coding: utf-8 -*-
"""
WhatsAppTranslator.spec
PyInstaller 打包配置文件（onedir 模式，更稳定）。

用法：
    pyinstaller --noconfirm --clean WhatsAppTranslator.spec

输出：
    dist/WhatsAppTranslator/WhatsAppTranslator.exe
"""

import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_all

block_cipher = None

hiddenimports = []
datas = []
binaries = []

# 收集所有依赖
for pkg in ['deep_translator', 'pystray', 'PIL', 'keyboard', 'uiautomation',
            'comtypes', 'requests', 'yaml', 'mss', 'pytesseract']:
    h, d, b = collect_all(pkg)
    hiddenimports += h
    datas += d
    binaries += b

hiddenimports += [
    'pystray._win32',
    'pystray._util',
    'deep_translator.google',
    'yaml',
    'yaml._yaml',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'tkinter.test', 'unittest', 'test',
        'pydoc_data',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WhatsAppTranslator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WhatsAppTranslator',
)
