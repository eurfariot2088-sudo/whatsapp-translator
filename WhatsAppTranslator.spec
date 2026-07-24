# -*- mode: python ; coding: utf-8 -*-
"""
WhatsAppTranslator.spec
PyInstaller 打包配置文件。

用法：
    pyinstaller WhatsAppTranslator.spec

输出：
    dist/WhatsAppTranslator/WhatsAppTranslator.exe   （--onedir 默认，启动更快）
    或加 --onefile 改为单文件模式

该 spec 显式声明了第三方库的 hidden imports，避免运行时 ImportError。
"""

import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
import yaml

block_cipher = None

hiddenimports = []
hiddenimports += collect_submodules('deep_translator')
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('PIL')
hiddenimports += collect_submodules('keyboard')
hiddenimports += collect_submodules('uiautomation')
hiddenimports += collect_submodules('comtypes')
hiddenimports += collect_submodules('requests')
hiddenimports += collect_submodules('mss')
hiddenimports += collect_submodules('pytesseract')

hiddenimports += ['yaml']
hiddenimports += ['pystray._win32', 'pystray._util']
hiddenimports += [
    'deep_translator.google',
    'deep_translator.base',
    'deep_translator.constants',
    'deep_translator.validate',
]

# ---- 收集数据文件（PIL 字体等） ----
datas = []
datas += collect_data_files('deep_translator')
datas += collect_data_files('PIL')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不必要的标准库以减小体积
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
    console=False,                # 不弹黑色控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                    # 如有 .ico 文件可改为 'assets/app.ico'
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
