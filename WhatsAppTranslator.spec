# -*- mode: python ; coding: utf-8 -*-
"""
WhatsAppTranslator.spec
PyInstaller 打包配置文件（onedir 模式）。

用法：
    pyinstaller --noconfirm --clean WhatsAppTranslator.spec

输出：
    dist/WhatsAppTranslator/WhatsAppTranslator.exe
"""

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'deep_translator',
        'deep_translator.google',
        'deep_translator.base',
        'deep_translator.constants',
        'deep_translator.validate',
        'pystray',
        'pystray._win32',
        'pystray._util',
        'PIL',
        'keyboard',
        'uiautomation',
        'comtypes',
        'comtypes.client',
        'requests',
        'yaml',
        'yaml._yaml',
        'mss',
        'pytesseract',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'tkinter.test',
        'unittest',
        'test',
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
