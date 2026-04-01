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
# HOW TO BUILD:
# cd IGScraper
# pyinstaller instagram_scraper.spec
# ─────────────────────────────────────────────────────────────────────────────

from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

block_cipher = None

# Collect ALL PyAV files: Python modules + FFmpeg DLLs + data files
# Without this, av imports successfully on the build machine but crashes
# at runtime on the target machine because the FFmpeg DLLs are missing.
av_datas, av_binaries, av_hiddenimports = collect_all("av")

# numpy is required by PyAV's to_ndarray() — collect it fully so it's in the EXE
np_datas, np_binaries, np_hiddenimports = collect_all("numpy")

a = Analysis(
    ['main.py'],
    pathex=[str(Path('.').resolve())],          # ← original robust path
    binaries=[*av_binaries, *np_binaries],
    datas=[
        # New PNG (splash + title-bar icon) → lands in root of _MEIPASS
        ('cansa_icon.png', '.'),

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

        # PyAV — required for Live Mirror H.264 decoding (collected automatically)
        *av_hiddenimports,

        # numpy — required by PyAV frame conversion
        *np_hiddenimports,

        # Mirror module
        'src.mirror', 'src.mirror.mirror_widget', 'src.mirror.stream_worker',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    console=False,                    # GUI only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='cansa_icon.ico',               # ← sets the .exe icon (PyInstaller converts PNG)
)