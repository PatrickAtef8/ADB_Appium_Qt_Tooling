# -*- mode: python ; coding: utf-8 -*-
# instagram_scraper.spec
# ─────────────────────────────────────────────────────────────────────────────
# PyInstaller spec for Cansa EXE (merged v1 + v2)
#
# • All original datas (config + assets)
# • Cansav2.png bundled in root (for splash + title-bar icon)
# • All original hiddenimports + new ones (PyQt6 + qfluentwidgets)
# • Icon set on the final EXE
#
# HOW TO BUILD:
# cd IGScraper
# pyinstaller instagram_scraper.spec
# ─────────────────────────────────────────────────────────────────────────────

from pathlib import Path

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[str(Path('.').resolve())],          # ← original robust path
    binaries=[],
    datas=[
        # New PNG (splash + title-bar icon) → lands in root of _MEIPASS
        ('Cansav2.png', '.'),

        # Original config & assets
        ('config/settings.json', 'config'),
        ('config/blacklist.json', 'config'),
        ('assets/credentials.json', 'assets'),
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
    icon='Cansav2.png',               # ← sets the .exe icon (PyInstaller converts PNG)
)