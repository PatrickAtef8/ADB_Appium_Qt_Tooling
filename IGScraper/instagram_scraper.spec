# -*- mode: python ; coding: utf-8 -*-
# instagram_scraper.spec
# ─────────────────────────────────────────────────────────────────────────────
# PyInstaller spec for Cansa EXE (merged v1 + v2)
#
# • All original datas (config + assets)
# • cansa_icon.png bundled in root (for splash + title-bar icon)
# • All original hiddenimports + new ones (PyQt6 + qfluentwidgets)
# • Icon set on the final EXE
#
# FIX (Windows splash PNG blank):
#   The previous version tried to manually copy the Qt imageformats plugin
#   DLLs (qpng.dll, qjpeg.dll …) into _MEIPASS\PyQt6\Qt6\plugins\imageformats\.
#   PyInstaller's own PyQt6 hook already does this correctly, so the manual
#   copy was redundant and occasionally placed the DLLs in the wrong slot,
#   causing Qt's plugin resolver to silently skip them.
#
#   The correct fix is a runtime hook (rthook_qt_plugins.py) that sets
#   QT_PLUGIN_PATH to _MEIPASS\PyQt6\Qt6\plugins\ before Qt is initialised.
#   Qt then finds every plugin — imageformats, platforms, styles — regardless
#   of the process working directory or qt.conf resolution order.
#
# HOW TO BUILD:
# cd IGScraper
# pyinstaller instagram_scraper.spec
# ─────────────────────────────────────────────────────────────────────────────

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Collect ALL PyAV files: Python modules + FFmpeg DLLs + data files
# Without this, av imports successfully on the build machine but crashes
# at runtime on the target machine because the FFmpeg DLLs are missing.
av_datas, av_binaries, av_hiddenimports = collect_all("av")

# numpy is required by PyAV's to_ndarray() — collect it fully so it's in the EXE
np_datas, np_binaries, np_hiddenimports = collect_all("numpy")

a = Analysis(
    ['main.py'],
    pathex=[str(Path('.').resolve())],
    binaries=[*av_binaries, *np_binaries],
    datas=[
        # PNG (splash logo) -> lands in root of _MEIPASS
        # Qt decodes it via the imageformats plugin (qpng.dll on Windows,
        # libqpng.so on Linux) whose path is registered by rthook_qt_plugins.py
        # before Qt is initialised — so QImageReader always finds it.
        ('cansa_icon.png', '.'),

        # ICO (app icon + emergency splash fallback) -> root of _MEIPASS
        # ICO is decoded by Qt's built-in reader — no plugin required.
        ('cansa_icon.ico', '.'),

        # Original config & assets
        ('config/settings.json', 'config'),
        ('config/blacklist.json', 'config'),
        ('assets/credentials.json', 'assets'),

        # scrcpy server JAR for Live Mirror feature
        ('src/mirror/assets/scrcpy-server.jar', 'src/mirror/assets'),

        *av_datas,
        *np_datas,
    ],
    hiddenimports=[
        # Original hiddenimports
        'PyQt6.QtCore', 'PyQt6.QtGui', 'PyQt6.QtWidgets',
        'gspread', 'google.auth', 'google.auth.transport.requests',
        'google_auth_oauthlib.flow', 'google.oauth2.credentials',
        'appium', 'appium.webdriver',
        'appium.options.android', 'appium.options.android.uiautomator2.base',
        'selenium', 'selenium.webdriver',
        'selenium.webdriver.support.ui',
        'selenium.webdriver.support.expected_conditions',
        'src.ui.main_window',
        'src.automation.appium_controller',
        'src.automation.appium_manager',
        'src.automation.scraper',
        'src.sheets.google_sheets',
        'src.utils.config_manager',
        'src.utils.filters',
        'src.utils.blacklist',

        # New additions from your updated spec
        'PyQt6',
        'qfluentwidgets',

        # PyAV -- required for Live Mirror H.264 decoding (collected automatically)
        *av_hiddenimports,

        # numpy -- required by PyAV frame conversion
        *np_hiddenimports,

        # Mirror module
        'src.mirror', 'src.mirror.mirror_widget', 'src.mirror.stream_worker',
    ],
    hookspath=[],
    hooksconfig={},
    # rthook_qt_plugins.py runs before main.py and sets QT_PLUGIN_PATH so Qt
    # finds imageformats/platforms plugins inside _MEIPASS on Windows.
    runtime_hooks=['rthook_qt_plugins.py'],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Cansa',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=true,                    # GUI only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='cansa_icon.ico',            # <- sets the .exe icon
)