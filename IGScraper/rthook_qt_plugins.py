# rthook_qt_plugins.py
# ─────────────────────────────────────────────────────────────────────────────
# PyInstaller runtime hook — executed BEFORE main.py and before PyQt6 imports.
#
# Problem this solves:
#   On Windows, when Qt starts inside a frozen EXE it must locate its plugin
#   DLLs (imageformats/qpng.dll, imageformats/qjpeg.dll, platforms/qwindows.dll
#   etc.) via its plugin search path.  PyInstaller's built-in PyQt6 hook copies
#   those DLLs correctly into _MEIPASS\PyQt6\Qt6\plugins\, but Qt's runtime
#   path resolver sometimes fails to find them when the process working directory
#   is not _MEIPASS — producing a silently null QPixmap for every PNG/JPG loaded
#   through QImageReader or QPixmap, while ICO (built-in Qt reader, no plugin
#   needed) continues to work.  This is exactly why the splash PNG was invisible
#   on Windows but the app-bar ICO was fine.
#
# Fix:
#   Set QT_PLUGIN_PATH before Qt is initialised so it always finds the bundled
#   plugins regardless of the working directory or qt.conf resolution order.
#   We also call QCoreApplication.addLibraryPath() as a belt-and-suspenders
#   measure for the platforms/ plugin (needed on Windows for the window manager).
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys

if hasattr(sys, "_MEIPASS"):
    _plugin_path = os.path.join(sys._MEIPASS, "PyQt6", "Qt6", "plugins")
    if os.path.isdir(_plugin_path):
        # Environment variable is read by Qt during QCoreApplication construction.
        # Setting it here (pre-import) guarantees it is visible to Qt's resolver.
        os.environ["QT_PLUGIN_PATH"] = _plugin_path
