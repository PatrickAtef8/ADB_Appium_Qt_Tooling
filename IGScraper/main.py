"""
Instagram Follower Scraper
Entry point — launches the PyQt6 GUI.

Uses setWindowOpacity() for all fade animations — this delegates to the OS
compositor and is hardware-accelerated, giving perfectly smooth transitions
with no lag or jank.

Sequence:
  1. MainWindow built maximized, hidden.
  2. SplashWindow shown maximized instantly at full opacity.
  3. After hold, splash fades OUT smoothly via setWindowOpacity.
  4. MainWindow fades IN smoothly via setWindowOpacity.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
from PyQt6.QtGui     import QIcon, QPixmap, QScreen
from PyQt6.QtCore    import Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtSignal, QRect

from src.ui.main_window import MainWindow


_APP_BG = "#0f172a"   # must match dark `bg` in MainWindow._apply_stylesheet


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3: Resource path helper for PyInstaller bundled EXE
# When running as a frozen EXE sys._MEIPASS is the temp extraction folder.
# At development time we fall back to the directory containing this file.
# ─────────────────────────────────────────────────────────────────────────────

def _resource_path(relative: str) -> str:
    """Return absolute path to a bundled resource, works for dev and PyInstaller EXE."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


# ─────────────────────────────────────────────────────────────────────────────
# SplashWindow
# ─────────────────────────────────────────────────────────────────────────────

class SplashWindow(QWidget):
    finished = pyqtSignal()

    _HOLD_MS     = 2200
    _FADE_OUT_MS = 600

    def __init__(self, icon_path: str, screen_geo: QRect):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool               |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setGeometry(screen_geo)
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"QWidget {{ background-color: {_APP_BG}; }}")

        # ── Content ───────────────────────────────────────────────────────
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(28)
        layout.addStretch(1)

        logo_lbl = QLabel(self)
        logo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(icon_path)
        if not pixmap.isNull():
            pixmap = pixmap.scaled(
                180, 180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        logo_lbl.setPixmap(pixmap)
        logo_lbl.setStyleSheet("background: transparent;")

        title_lbl = QLabel("Cansa", self)
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet(
            "background: transparent;"
            "color: #f8fafc;"
            "font-family: 'Inter', 'Segoe UI', sans-serif;"
            "font-size: 48pt;"
            "font-weight: 700;"
            "letter-spacing: 6px;"
        )

        tag_lbl = QLabel("Instagram Automation Suite", self)
        tag_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tag_lbl.setStyleSheet(
            "background: transparent;"
            "color: #3b82f6;"
            "font-family: 'Inter', 'Segoe UI', sans-serif;"
            "font-size: 14pt;"
            "font-weight: 500;"
            "letter-spacing: 3px;"
        )

        layout.addWidget(logo_lbl)
        layout.addWidget(title_lbl)
        layout.addWidget(tag_lbl)
        layout.addStretch(1)

        self._bar = QWidget(self)
        self._bar.setFixedHeight(3)
        self._bar.setStyleSheet("background: #3b82f6; border-radius: 1px;")

        # ── Fade-out via setWindowOpacity (OS compositor — smooth) ────────
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._bar.setGeometry(0, self.height() - 3, self.width(), 3)

    def start(self):
        self.setWindowOpacity(1.0)
        self.showMaximized()
        self._hold_timer.start()

    def _on_done(self):
        self.finished.emit()
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Cansa")
    app.setApplicationVersion("1.0")

    # FIX 3: Use _resource_path so the icon is found inside the EXE bundle
    icon_path = _resource_path("Cansav2.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
        print(f"✅ Application icon loaded: {icon_path}")
    else:
        print(f"⚠️  WARNING: Cansav2.png not found at:\n   {icon_path}")

    # ── Build MainWindow maximized, hidden, opacity 0 ─────────────────────
    window = MainWindow()
    window.resize(1600, 1000)
    window.setWindowOpacity(0.0)

    screen: QScreen = QApplication.primaryScreen()
    screen_geo = screen.availableGeometry()

    splash = SplashWindow(icon_path, screen_geo)

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