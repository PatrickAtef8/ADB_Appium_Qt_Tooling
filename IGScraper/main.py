"""
Instagram Follower Scraper
Entry point — launches the PyQt6 GUI.
"""

import sys
import os
import traceback
import datetime
import pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Global crash log ─────────────────────────────────────────────────────────
# On Windows: writes to %USERPROFILE%\cansa_crash.log (always writable)
# On Linux:   writes to ~/cansa_crash.log
_LOG_FILE = pathlib.Path.home() / "cansa_crash.log"

def _write_crash(msg: str):
    try:
        with open(_LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n=== {datetime.datetime.now()} ===\n{msg}\n")
    except Exception:
        pass

def _excepthook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _write_crash(msg)
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _excepthook

# Also catch threading exceptions (QThread run() exceptions end up here)
import threading
def _thread_excepthook(args):
    msg = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    _write_crash(f"[Thread: {args.thread}]\n{msg}")
threading.excepthook = _thread_excepthook

from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
from PyQt6.QtGui import QIcon, QPixmap, QScreen, QImageReader
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtSignal, QRect

# ── Windows DPI: prevent Qt from scaling up beyond 100% on 96-dpi screens ────
# Must be set BEFORE QApplication is constructed.
QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)

# ── Cross-platform font scaling ───────────────────────────────────────────────
# Windows GDI renders the same point-size fonts ~15 % larger than Linux/X11.
# _pts() corrects for this so the splash and main window look identical on
# both platforms.
def _pts(base_pt: int) -> int:
    if sys.platform == "win32":
        return max(6, round(base_pt * 0.85))
    return base_pt

from src.ui.main_window import MainWindow


_APP_BG = "#0f172a"


# ─────────────────────────────────────────────────────────────────────────────
# Resource helper (PyInstaller compatible)
# ─────────────────────────────────────────────────────────────────────────────
def _resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


# ─────────────────────────────────────────────────────────────────────────────
# Splash Window
# ─────────────────────────────────────────────────────────────────────────────
class SplashWindow(QWidget):
    finished = pyqtSignal()

    _HOLD_MS = 2200
    _FADE_OUT_MS = 600

    def __init__(self, image_path: str, screen_geo: QRect):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setGeometry(screen_geo)
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"QWidget {{ background-color: {_APP_BG}; }}")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(20)

        logo_lbl = QLabel(self)
        logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ── Cross-platform image loading ──────────────────────────────────
        # On Windows, QIcon(path).pixmap() can silently return a null pixmap
        # before a native window is shown (the Windows platform plugin is not
        # yet fully initialised at widget-construction time).
        #
        # QImageReader is the correct fix: it talks directly to the format
        # plugin (qpng.dll / libqpng.so) and does NOT need a screen or window
        # handle.  ICO files are decoded by Qt's built-in reader (no plugin
        # required), so the QIcon fallback handles them fine.
        #
        # Load order:
        #   1. QImageReader  — reliable on both platforms for PNG.
        #   2. QIcon fallback — catches ICO and any edge-case PNG miss.
        pixmap = QPixmap()

        if image_path.lower().endswith(".png"):
            reader = QImageReader(image_path)
            reader.setAutoTransform(True)
            img = reader.read()
            if not img.isNull():
                pixmap = QPixmap.fromImage(img)
            else:
                print(f"⚠️ QImageReader error ({image_path}): {reader.errorString()} — trying QIcon fallback")

        if pixmap.isNull():
            # Fallback: works for ICO (built-in) and sometimes PNG on Linux
            icon = QIcon(image_path)
            if not icon.isNull():
                pixmap = icon.pixmap(256, 256)

        if not pixmap.isNull():
            src_w, src_h = pixmap.width(), pixmap.height()
            if src_w > 300 or src_h > 300:
                pixmap = pixmap.scaled(
                    300, 300,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        else:
            print(f"⚠️ Splash image could not be loaded: {image_path}")

        # Store for possible deferred reload on Windows.
        # _logo_loaded_ok is True only when we KNOW the pixmap rendered correctly
        # (loaded via QImageReader and non-null with real dimensions).
        # On Windows, the pre-show QIcon fallback can produce a non-null but
        # visually blank pixmap, so we must NOT rely solely on isNull() to decide
        # whether to retry after the window is shown.
        self._logo_lbl       = logo_lbl
        self._image_path     = image_path
        self._pixmap         = pixmap
        # PNG loaded via QImageReader before show() is reliable — mark as ok.
        # QIcon fallback (ICO / edge-case PNG) is NOT reliable on Windows pre-show.
        self._logo_loaded_ok = (
            not pixmap.isNull()
            and image_path.lower().endswith(".png")
            and pixmap.width() > 1
        )

        logo_lbl.setPixmap(pixmap)
        logo_lbl.setStyleSheet("background: transparent;")

        title_lbl = QLabel("Cansa", self)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            f"color: #f8fafc;"
            f"font-size: {_pts(48)}pt;"
            f"font-weight: 700;"
            f"letter-spacing: 6px;"
            f"background: transparent;"
        )

        tag_lbl = QLabel("Instagram Automation Suite", self)
        tag_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tag_lbl.setStyleSheet(
            f"color: #3b82f6;"
            f"font-size: {_pts(14)}pt;"
            f"letter-spacing: 3px;"
            f"background: transparent;"
        )

        layout.addWidget(logo_lbl)
        layout.addWidget(title_lbl)
        layout.addWidget(tag_lbl)

        self._anim_out = QPropertyAnimation(self, b"windowOpacity", self)
        self._anim_out.setDuration(self._FADE_OUT_MS)
        self._anim_out.setStartValue(1.0)
        self._anim_out.setEndValue(0.0)
        self._anim_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim_out.finished.connect(self._on_done)

        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(self._HOLD_MS)
        self._hold_timer.timeout.connect(self._anim_out.start)

    def start(self):
        self.setWindowOpacity(1.0)
        self.showMaximized()
        # On Windows the native window handle does not exist until after show().
        # Defer a second load attempt so QImageReader / QIcon have a valid
        # platform context — this fixes the blank logo on Windows.
        QTimer.singleShot(50, self._ensure_logo_loaded)
        self._hold_timer.start()

    def _ensure_logo_loaded(self):
        """Re-attempt image load now that the window is fully shown.

        On Windows the platform plugin (Qt's windows backend) is not fully
        initialised until after the first native window is shown.  Any pixmap
        loaded *before* show() — even via QImageReader — may render as a blank
        transparent image.  We therefore always retry with QImageReader here
        (post-show) and only skip if we already confirmed a good load earlier.
        """
        if self._logo_loaded_ok:
            return   # already confirmed a clean load

        path = self._image_path
        pixmap = QPixmap()

        # QImageReader is reliable post-show on all platforms.
        if path.lower().endswith(".png"):
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            img = reader.read()
            if not img.isNull():
                pixmap = QPixmap.fromImage(img)
            else:
                print(f"⚠️ QImageReader post-show error ({path}): {reader.errorString()}")

        # ICO / any remaining miss — QIcon is reliable once a window exists.
        if pixmap.isNull():
            icon = QIcon(path)
            if not icon.isNull():
                pixmap = icon.pixmap(256, 256)

        if not pixmap.isNull():
            if pixmap.width() > 300 or pixmap.height() > 300:
                pixmap = pixmap.scaled(
                    300, 300,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            self._pixmap        = pixmap
            self._logo_loaded_ok = True
            self._logo_lbl.setPixmap(pixmap)
            self._logo_lbl.update()
        else:
            print(f"⚠️ Splash logo could not be loaded even post-show: {path}")

    def _on_done(self):
        self.finished.emit()
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Cansa")
    app.setApplicationVersion("1.0")

    # ── Paths ─────────────────────────────────────────────────────────────
    ico_path = _resource_path("cansa_icon.ico")
    png_path = _resource_path("cansa_icon.png")

    # ── App icon (Windows / Taskbar) ───────────────────────────────────────
    if os.path.exists(ico_path):
        app.setWindowIcon(QIcon(ico_path))
        print(f"✅ ICO loaded: {ico_path}")
    else:
        print(f"⚠️ ICO not found: {ico_path}")
        

    # ── Main Window ────────────────────────────────────────────────────────
    window = MainWindow()
    window.resize(1600, 1000)
    window.setWindowOpacity(0.0)

    screen: QScreen = QApplication.primaryScreen()
    screen_geo = screen.availableGeometry()

    # ── Splash image ───────────────────────────────────────────────────────
    # PNG is used on both platforms for a crisp, full-resolution logo.
    # On Windows frozen EXE this previously failed silently because PyInstaller
    # did not bundle Qt's imageformats plugin DLLs (qpng.dll etc.) — fixed in
    # the spec by explicitly including the imageformats folder.
    # ICO is kept only as an emergency fallback.
    splash_img = png_path if os.path.exists(png_path) else ico_path
    if not os.path.exists(splash_img):
        print(f"⚠️ Splash image not found: {splash_img}")
    else:
        print(f"✅ Splash image: {splash_img}")

    splash = SplashWindow(splash_img, screen_geo)

    def _reveal_main():
        anim = QPropertyAnimation(window, b"windowOpacity", window)
        anim.setDuration(500)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        window.showMaximized()
        anim.start()

    splash.finished.connect(_reveal_main)
    splash.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()