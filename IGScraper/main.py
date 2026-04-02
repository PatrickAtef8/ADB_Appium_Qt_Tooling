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
_LOG_FILE   = pathlib.Path.home() / "cansa_crash.log"
_SPLASH_LOG = pathlib.Path.home() / "splash_debug.log"

def _write_crash(msg: str):
    try:
        with open(_LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n=== {datetime.datetime.now()} ===\n{msg}\n")
    except Exception:
        pass

# ── Splash debug logger ───────────────────────────────────────────────────────
def _slog(msg: str):
    """Write a timestamped line to splash_debug.log (always writable)."""
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')}] {msg}\n"
    try:
        with open(_SPLASH_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
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

QApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)

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
# Pixmap loader — logs every step to splash_debug.log
# ─────────────────────────────────────────────────────────────────────────────
def _load_pixmap(png_path: str, ico_path: str, size: int = 300,
                 label: str = "") -> QPixmap:
    prefix = f"[{label}] " if label else ""
    pixmap = QPixmap()

    # ── Step 1: PNG via QImageReader ─────────────────────────────────────────
    png_exists = os.path.exists(png_path)
    _slog(f"{prefix}PNG path: {png_path}  exists={png_exists}")

    if png_exists:
        _slog(f"{prefix}  file size: {os.path.getsize(png_path)} bytes")
        supported = [fmt.data().decode() for fmt in QImageReader.supportedImageFormats()]
        _slog(f"{prefix}  Qt supported formats: {supported}")
        reader = QImageReader(png_path)
        reader.setAutoTransform(True)
        img = reader.read()
        if not img.isNull():
            pixmap = QPixmap.fromImage(img)
            _slog(f"{prefix}  QImageReader OK → pixmap {pixmap.width()}x{pixmap.height()}  isNull={pixmap.isNull()}")
        else:
            _slog(f"{prefix}  QImageReader FAILED: '{reader.errorString()}'")

    # ── Step 2: PNG via QIcon fallback ───────────────────────────────────────
    if pixmap.isNull() and png_exists:
        icon = QIcon(png_path)
        _slog(f"{prefix}  QIcon(png) isNull={icon.isNull()}")
        if not icon.isNull():
            pixmap = icon.pixmap(size, size)
            _slog(f"{prefix}  QIcon(png) → pixmap {pixmap.width()}x{pixmap.height()}  isNull={pixmap.isNull()}")

    # ── Step 3: ICO via QIcon (guaranteed built-in decoder) ──────────────────
    if pixmap.isNull():
        ico_exists = os.path.exists(ico_path)
        _slog(f"{prefix}  ICO path: {ico_path}  exists={ico_exists}")
        if ico_exists:
            _slog(f"{prefix}  ICO file size: {os.path.getsize(ico_path)} bytes")
            icon = QIcon(ico_path)
            _slog(f"{prefix}  QIcon(ico) isNull={icon.isNull()}")
            if not icon.isNull():
                pixmap = icon.pixmap(size, size)
                _slog(f"{prefix}  QIcon(ico) → pixmap {pixmap.width()}x{pixmap.height()}  isNull={pixmap.isNull()}")

    if pixmap.isNull():
        _slog(f"{prefix}  *** ALL METHODS FAILED — pixmap is null ***")
        return pixmap

    # ── Scale if needed ───────────────────────────────────────────────────────
    _slog(f"{prefix}  before scale: {pixmap.width()}x{pixmap.height()}")
    if pixmap.width() > size or pixmap.height() > size:
        pixmap = pixmap.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    _slog(f"{prefix}  FINAL pixmap: {pixmap.width()}x{pixmap.height()}  isNull={pixmap.isNull()}")
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

        _slog("--- PRE-SHOW load ---")
        pixmap = _load_pixmap(png_path, ico_path, self._LOGO_SIZE, label="pre-show")
        if not pixmap.isNull():
            logo_lbl.setPixmap(pixmap)
            _slog(f"pre-show: setPixmap done")
        else:
            _slog("pre-show: pixmap null, skipping setPixmap")

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
        _slog(f"start(): showMaximized called  geometry={self.geometry()}")
        QTimer.singleShot(50, self._ensure_logo_loaded)
        self._hold_timer.start()

    def _ensure_logo_loaded(self):
        _slog("--- POST-SHOW load (50ms after showMaximized) ---")
        _slog(f"  logo_lbl geometry={self._logo_lbl.geometry()}  size={self._logo_lbl.size()}")
        _slog(f"  splash geometry={self.geometry()}")

        pixmap = _load_pixmap(self._png_path, self._ico_path, self._LOGO_SIZE, label="post-show")

        if not pixmap.isNull():
            self._logo_lbl.setPixmap(pixmap)
            self._logo_lbl.update()
            self._logo_lbl.repaint()
            _slog(f"post-show: setPixmap+repaint done  label_size={self._logo_lbl.size()}")
        else:
            _slog("post-show: *** ALL METHODS FAILED — splash will show no image ***")

    def _on_done(self):
        self.finished.emit()
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
def main():
    # Fresh log every run
    try:
        _SPLASH_LOG.write_text(
            f"=== splash_debug.log started {datetime.datetime.now()} ===\n"
            f"Python: {sys.version}\n"
            f"Platform: {sys.platform}\n"
            f"Frozen (PyInstaller): {hasattr(sys, '_MEIPASS')}\n"
            f"_MEIPASS: {getattr(sys, '_MEIPASS', 'N/A')}\n\n",
            encoding="utf-8"
        )
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("Cansa")
    app.setApplicationVersion("1.0")

    ico_path = _resource_path("cansa_icon.ico")
    png_path = _resource_path("cansa_icon.png")

    _slog(f"ico_path: exists={os.path.exists(ico_path)}  {ico_path}")
    _slog(f"png_path: exists={os.path.exists(png_path)}  {png_path}")

    if os.path.exists(ico_path):
        app.setWindowIcon(QIcon(ico_path))
        _slog("App icon set from ICO OK")
    else:
        _slog("WARNING: ICO not found for app icon")

    window = MainWindow()
    window.resize(1600, 1000)
    window.setWindowOpacity(0.0)

    screen: QScreen = QApplication.primaryScreen()
    screen_geo = screen.availableGeometry()
    _slog(f"screen: geo={screen_geo}  dpr={screen.devicePixelRatio()}")

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