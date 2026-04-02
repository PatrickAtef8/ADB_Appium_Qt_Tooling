# -*- mode: python ; coding: utf-8 -*-
# instagram_scraper.spec
# ─────────────────────────────────────────────────────────────────────────────
# PyInstaller spec for Cansa EXE (merged v1 + v2)
#
# • All original datas (config + assets)
# • cansa_icon.png bundled in root (for splash + title-bar icon)
# • All original hiddenimports + new ones (PyQt6 + qfluentwidgets)
# • Icon set on the final EXE
# • Qt imageformats plugins explicitly bundled so QPixmap can load PNG/JPG
#   inside the frozen EXE on Windows (without these DLLs PNG returns null)
#
# HOW TO BUILD:
# cd IGScraper
# pyinstaller instagram_scraper.spec
# ─────────────────────────────────────────────────────────────────────────────

import os
import PyQt6
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

block_cipher = None

# ── Qt imageformats plugins ───────────────────────────────────────────────────
# PyInstaller does NOT automatically bundle Qt's image-format plugin DLLs
# (qjpeg.dll, qpng.dll, etc.).  Without them, QImageReader / QPixmap silently
# returns a null image for PNG/JPG files inside the frozen EXE on Windows —
# the file is bundled but cannot be decoded.  ICO works without a plugin
# because Qt has a built-in ICO reader, which is why the app-icon showed but
# the splash PNG did not.
#
# We probe multiple candidate paths because PyQt6's internal layout differs
# between pip versions (Qt6/ vs Qt/ vs directly under the package root).
_qt_pkg_dir = os.path.dirname(PyQt6.__file__)
_imageformats_src = None
for _candidate in [
    os.path.join(_qt_pkg_dir, "Qt6", "plugins", "imageformats"),
    os.path.join(_qt_pkg_dir, "Qt",  "plugins", "imageformats"),
    os.path.join(_qt_pkg_dir, "plugins", "imageformats"),
]:
    if os.path.isdir(_candidate):
        _imageformats_src = _candidate
        break

if _imageformats_src is None:
    import warnings
    warnings.warn(
        "⚠️  Could not locate PyQt6 imageformats plugin folder — "
        "PNG/JPG splash image may not display in the frozen EXE on Windows. "
        f"Searched under: {_qt_pkg_dir}"
    )

# Destination inside _MEIPASS must match Qt's expected plugin search path.
_imageformats_dst = os.path.join("PyQt6", "Qt6", "plugins", "imageformats")

# Collect ALL PyAV files: Python modules + FFmpeg DLLs + data files
# Without this, av imports successfully on the build machine but crashes
# at runtime on the target machine because the FFmpeg DLLs are missing.
av_datas, av_binaries, av_hiddenimports = collect_all("av")

# numpy is required by PyAV's to_ndarray() — collect it fully so it's in the EXE
np_datas, np_binaries, np_hiddenimports = collect_all("numpy")

a = Analysis(
    ['main.py'],
    pathex=[str(Path('.').resolve())],          # <- original robust path
    binaries=[*av_binaries, *np_binaries],
    datas=[
        # PNG (splash logo) -> lands in root of _MEIPASS
        # Decoded by the imageformats plugin bundle below.
        ('cansa_icon.png', '.'),

        # ICO (app icon + emergency splash fallback) -> root of _MEIPASS
        # ICO is decoded by Qt's built-in reader — no plugin required.
        ('cansa_icon.ico', '.'),

        # Qt image-format plugins: lets QImageReader load PNG/JPG in frozen EXE.
        # _imageformats_src is None when the folder wasn't found (warning
        # already emitted above); skip the entry in that case to avoid a
        # PyInstaller error on the tuple.
        *([(_imageformats_src, _imageformats_dst)] if _imageformats_src else []),

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
    icon='cansa_icon.ico',               # <- sets the .exe icon
)