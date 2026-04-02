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
# Reliable cross-platform pixmap loader
# ─────────────────────────────────────────────────────────────────────────────
def _load_pixmap(png_path: str, ico_path: str, size: int = 300) -> QPixmap:
    """
    Load the logo pixmap reliably on both platforms and in frozen PyInstaller EXEs.

    Load order:
      1. QImageReader on PNG  — reliable on Linux; works on Windows post-show.
      2. QIcon on PNG         — fallback if QImageReader misses.
      3. QIcon on ICO         — built-in Qt decoder, no plugin required.
                                Guaranteed to work on Windows frozen EXEs.
                                Same mechanism used by the app icon and title bar
                                icon, which always work correctly.
    """
    pixmap = QPixmap()

    # 1. PNG via QImageReader
    if os.path.exists(png_path):
        reader = QImageReader(png_path)
        reader.setAutoTransform(True)
        img = reader.read()
        if not img.isNull():
            pixmap = QPixmap.fromImage(img)

    # 2. PNG via QIcon fallback
    if pixmap.isNull() and os.path.exists(png_path):
        icon = QIcon(png_path)
        if not icon.isNull():
            pixmap = icon.pixmap(size, size)

    # 3. ICO via QIcon — built-in decoder, always works on Windows frozen EXE
    if pixmap.isNull() and os.path.exists(ico_path):
        icon = QIcon(ico_path)
        if not icon.isNull():
            pixmap = icon.pixmap(size, size)

    if pixmap.isNull():
        return pixmap

    # Scale down if needed, preserve aspect ratio
    if pixmap.width() > size or pixmap.height() > size:
        pixmap = pixmap.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    return pixmap


# ─────────────────────────────────────────────────────────────────────────────
# Splash Window
# ─────────────────────────────────────────────────────────────────────────────
class SplashWindow(QWidget):
    finished = pyqtSignal()

    _HOLD_MS     = 2200
    _FADE_OUT_MS = 600
    _LOGO_SIZE   = 300

    def __init__(self, png_path: str, ico_path: str, screen_geo: QRect):
        super().__init__()

        self._png_path = png_path
        self._ico_path = ico_path

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
        logo_lbl.setStyleSheet("background: transparent;")

        # Pre-show load — may be null on Windows before native window exists;
        # _ensure_logo_loaded() will reliably fix it 50ms after show().
        pixmap = _load_pixmap(png_path, ico_path, self._LOGO_SIZE)
        if not pixmap.isNull():
            logo_lbl.setPixmap(pixmap)

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

        self._logo_lbl = logo_lbl

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
        # Post-show reload ensures the image is set after the Windows platform
        # plugin is fully initialised. repaint() forces an immediate redraw so
        # the image is visible before the hold timer fires the fade-out.
        QTimer.singleShot(50, self._ensure_logo_loaded)
        self._hold_timer.start()

    def _ensure_logo_loaded(self):
        """
        Re-attempt image load 50ms after the window is shown.

        On Windows, Qt's platform plugin is not fully initialised until after
        the first native window is shown. Loading the pixmap here (post-show)
        is always reliable. repaint() forces an immediate synchronous redraw
        rather than waiting for the next event loop cycle.
        """
        pixmap = _load_pixmap(self._png_path, self._ico_path, self._LOGO_SIZE)
        if not pixmap.isNull():
            self._logo_lbl.setPixmap(pixmap)
            self._logo_lbl.update()
            self._logo_lbl.repaint()

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

    ico_path = _resource_path("cansa_icon.ico")
    png_path = _resource_path("cansa_icon.png")

    # ── App icon (taskbar / window manager) ───────────────────────────────────
    if os.path.exists(ico_path):
        app.setWindowIcon(QIcon(ico_path))

    # ── Main Window ────────────────────────────────────────────────────────────
    window = MainWindow()
    window.resize(1600, 1000)
    window.setWindowOpacity(0.0)

    screen: QScreen = QApplication.primaryScreen()
    screen_geo = screen.availableGeometry()

    # ── Splash ─────────────────────────────────────────────────────────────────
    splash = SplashWindow(png_path, ico_path, screen_geo)

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