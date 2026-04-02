from __future__ import annotations

from typing import Optional

from PyQt6.QtCore    import Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui     import QColor, QFont, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from qfluentwidgets import isDarkTheme

from .stream_worker import MirrorStreamWorker


# ─────────────────────────────────────────────────────────────────────────────
# Cross-platform scaling helpers (mirrors main_window.py)
# ─────────────────────────────────────────────────────────────────────────────
import sys as _sys

def _dpi_scale() -> float:
    return 0.85 if _sys.platform == "win32" else 1.0

def _pts(base_pt: int) -> int:
    return max(6, round(base_pt * _dpi_scale()))

def _px(base_px: int) -> int:
    return max(1, round(base_px * _dpi_scale()))




# ── accent palette (state colors only) ───────────────────────────────────────
_C = {
    "bg":          "#0a0f1e",
    "header":      "#1e293b",
    "border":      "#334155",
    "text_dim":    "#64748b",
    "text_main":   "#f8fafc",
    "idle":        "#64748b",
    "connecting":  "#f59e0b",
    "streaming":   "#22c55e",
    "disconnected":"#64748b",
    "error":       "#ef4444",
}

_ICONS = {
    "idle":         "📵",
    "connecting":   "⏳",
    "streaming":    "▶",
    "disconnected": "📴",
    "error":        "⚠",
}

_LABELS = {
    "idle":         "No stream — press  👁 View",
    "connecting":   "Connecting…",
    "streaming":    "● Live",
    "disconnected": "Disconnected",
    "error":        "Error",
}


def _font(size: int = 9, bold: bool = False) -> QFont:
    f = QFont("Segoe UI")
    f.setPointSize(size)
    if bold:
        f.setWeight(QFont.Weight.DemiBold)
    return f


# ── overlay (now fully theme-aware) ──────────────────────────────────────────
class _Overlay(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._state = "idle"
        self._msg   = _LABELS["idle"]

    def set_state(self, state: str, msg: str = ""):
        self._state = state
        self._msg   = msg or _LABELS.get(state, state)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dark = isDarkTheme()
        bg = "#0a0f1e" if dark else "#f8fafc"
        text_dim = "#64748b" if dark else "#94a3b8"

        p.fillRect(self.rect(), QColor(bg))

        # icon
        icon_font = QFont("Segoe UI Emoji", _pts(30))
        p.setFont(icon_font)
        p.setPen(QColor(_C.get(self._state, _C["idle"])))
        icon_rect = self.rect().adjusted(0, -40, 0, -40)
        p.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter,
                   _ICONS.get(self._state, "?"))

        # message
        p.setFont(_font(_pts(10)))
        p.setPen(QColor(text_dim))
        msg_rect = self.rect().adjusted(12, 44, -12, 0)
        p.drawText(msg_rect,
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                   self._msg)
        p.end()


# ── main widget ───────────────────────────────────────────────────────────────
class MirrorWidget(QFrame):
    """Embedded mirror panel for one Android device."""

    MIN_W, MIN_H = _px(240), _px(426)

    detached = pyqtSignal()

    def __init__(self, phone_index: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.phone_index = phone_index
        self._serial: Optional[str]                = None
        self._worker: Optional[MirrorStreamWorker] = None
        self._state  = "idle"
        self._session_id = 0

        self._C = _C.copy()
        self._update_palette()

        self._build_ui()
        self._set_state("idle")

    # ── theme support ───────────────────────────────────────────────────────
    def _update_palette(self):
        dark = isDarkTheme()
        self._C = _C.copy()
        if not dark:
            self._C.update({
                "bg":          "#f8fafc",
                "header":      "#f1f5f9",
                "border":      "#e2e8f0",
                "text_dim":    "#94a3b8",
                "text_main":   "#0f172a",
            })

    def _get_button_style(self) -> str:
        dark = isDarkTheme()
        if dark:
            return f"""
                QPushButton {{
                    background:#334155; color:{self._C['text_main']};
                    border:1px solid #475569; border-radius:4px; padding:0 8px;
                }}
                QPushButton:hover {{ background:#dc2626; border-color:#991b1b; }}
            """
        else:
            return f"""
                QPushButton {{
                    background:#e2e8f0; color:{self._C['text_main']};
                    border:1px solid #cbd5e1; border-radius:4px; padding:0 8px;
                }}
                QPushButton:hover {{ background:#dc2626; border-color:#991b1b; }}
            """

    def update_theme(self):
        """Call this when the app theme changes (Dark ↔ Light)"""
        self._update_palette()
        self.setStyleSheet(f"""
            MirrorWidget {{
                background: {self._C['bg']};
                border: 1px solid {self._C['border']};
                border-radius: 8px;
            }}
        """)
        if hasattr(self, '_header'):
            self._header.setStyleSheet(f"background:{self._C['header']};border-radius:0;")
        if hasattr(self, '_footer'):
            self._footer.setStyleSheet(f"background:{self._C['header']};")
        if hasattr(self, '_lbl_name'):
            self._lbl_name.setStyleSheet(f"color:{self._C['text_main']};background:transparent;")
        if hasattr(self, '_lbl_state'):
            colour = self._C.get(self._state, self._C["idle"])
            self._lbl_state.setStyleSheet(f"color:{colour};background:transparent;")
        if hasattr(self, '_lbl_fps'):
            self._lbl_fps.setStyleSheet(f"color:{self._C['text_dim']};background:transparent;")
        if hasattr(self, '_btn_disconnect'):
            self._btn_disconnect.setStyleSheet(self._get_button_style())
        if hasattr(self, '_video'):
            self._video.setStyleSheet(f"background:{self._C['bg']};border:none;")
        if hasattr(self, '_overlay') and self._overlay.isVisible():
            self._overlay.update()

    def update_phone_index(self, phone_index: int):
        """Fix: update label when switching between Phone 1 / Phone 2 / Phone 3"""
        self.phone_index = phone_index
        if hasattr(self, '_lbl_name'):
            self._lbl_name.setText(f"Phone {phone_index + 1}")

    # ── UI construction ─────────────────────────────────────────────────────
    def _build_ui(self):
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f"""
            MirrorWidget {{
                background: {self._C['bg']};
                border: 1px solid {self._C['border']};
                border-radius: 8px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── header ─────────────────────────────────────────────────────
        hdr = QWidget(self)
        self._header = hdr
        hdr.setFixedHeight(30)
        hdr.setStyleSheet(f"background:{self._C['header']};border-radius:0;")
        hlay = QHBoxLayout(hdr)
        hlay.setContentsMargins(8, 0, 8, 0)

        self._lbl_name  = QLabel(f"Phone {self.phone_index + 1}", hdr)
        self._lbl_name.setFont(_font(_pts(9), bold=True))
        self._lbl_name.setStyleSheet(f"color:{self._C['text_main']};background:transparent;")

        self._lbl_state = QLabel("idle", hdr)
        self._lbl_state.setFont(_font(_pts(9)))
        self._lbl_state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._lbl_state.setStyleSheet(f"color:{self._C['idle']};background:transparent;")

        hlay.addWidget(self._lbl_name)
        hlay.addStretch()
        hlay.addWidget(self._lbl_state)
        root.addWidget(hdr)

        # ── video surface ─────────────────────────────────────────────
        self._video = QLabel(self)
        self._video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._video.setStyleSheet(f"background:{self._C['bg']};border:none;")
        self._video.setScaledContents(False)
        root.addWidget(self._video, stretch=1)

        self._overlay = _Overlay(self._video)
        self._overlay.show()

        # ── footer ────────────────────────────────────────────────────
        ftr = QWidget(self)
        self._footer = ftr
        ftr.setFixedHeight(30)
        ftr.setStyleSheet(f"background:{self._C['header']};")
        flay = QHBoxLayout(ftr)
        flay.setContentsMargins(8, 2, 8, 2)

        self._lbl_fps = QLabel("", ftr)
        self._lbl_fps.setFont(_font(_pts(8)))
        self._lbl_fps.setStyleSheet(f"color:{self._C['text_dim']};background:transparent;")

        self._btn_disconnect = QPushButton("✖ Disconnect", ftr)
        self._btn_disconnect.setFont(_font(_pts(8), bold=True))
        self._btn_disconnect.setFixedHeight(22)
        self._btn_disconnect.setStyleSheet(self._get_button_style())
        self._btn_disconnect.clicked.connect(self.detach)
        self._btn_disconnect.hide()

        flay.addWidget(self._lbl_fps)
        flay.addStretch()
        flay.addWidget(self._btn_disconnect)
        root.addWidget(ftr)

    # ── public API ─────────────────────────────────────────────────────
    def attach(self, serial: str):
        self._session_id += 1
        my_session = self._session_id

        if self._worker is not None:
            old = self._worker
            self._worker = None
            try:
                old.frame_ready.disconnect()
                old.state_changed.disconnect()
                old.fps_updated.disconnect()
                old.finished.disconnect()
            except Exception:
                pass
            old.request_stop()

        self._serial = serial
        self._set_state("connecting")
        self._lbl_fps.setText("")
        self._btn_disconnect.show()

        w = MirrorStreamWorker(serial=serial, parent=None)

        def _guarded_frame(img: QImage, sid=my_session):
            if sid == self._session_id:
                self._on_frame(img)

        def _guarded_state(state: str, sid=my_session):
            if sid == self._session_id:
                self._on_state(state)

        def _guarded_fps(fps: float, sid=my_session):
            if sid == self._session_id:
                self._on_fps(fps)

        def _guarded_finished(sid=my_session):
            if sid == self._session_id:
                self._on_worker_finished()

        w.frame_ready.connect(_guarded_frame, Qt.ConnectionType.QueuedConnection)
        w.state_changed.connect(_guarded_state, Qt.ConnectionType.QueuedConnection)
        w.fps_updated.connect(_guarded_fps, Qt.ConnectionType.QueuedConnection)
        w.finished.connect(_guarded_finished, Qt.ConnectionType.QueuedConnection)

        self._worker = w
        w.start()

    def detach(self):
        self._session_id += 1
        if self._worker is not None:
            old = self._worker
            self._worker = None
            try:
                old.frame_ready.disconnect()
                old.state_changed.disconnect()
                old.fps_updated.disconnect()
                old.finished.disconnect()
            except Exception:
                pass
            old.request_stop()

        self._serial = None
        self._set_state("disconnected")
        self._lbl_fps.setText("")
        self._btn_disconnect.hide()
        self.detached.emit()

    @property
    def is_active(self) -> bool:
        return self._state in ("connecting", "streaming")

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    # ── slots ──────────────────────────────────────────────────────────
    @pyqtSlot(QImage)
    def _on_frame(self, img: QImage):
        sz  = self._video.size()
        pix = QPixmap.fromImage(img).scaled(
            sz,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._video.setPixmap(pix)

        if self._state != "streaming":
            self._state = "streaming"
            colour = self._C["streaming"]
            self._lbl_state.setText(_LABELS["streaming"])
            self._lbl_state.setStyleSheet(f"color:{colour};background:transparent;")
            if self._overlay.isVisible():
                self._overlay.hide()

    @pyqtSlot(str)
    def _on_state(self, state: str):
        if state.startswith("error:"):
            self._set_state("error", state[6:])
            self._btn_disconnect.show()
        else:
            self._set_state(state)
            if state in ("disconnected", "error"):
                self._btn_disconnect.hide()

    @pyqtSlot(float)
    def _on_fps(self, fps: float):
        self._lbl_fps.setText(f"{fps:.1f} fps")

    @pyqtSlot()
    def _on_worker_finished(self):
        if self._state not in ("disconnected", "idle", "error"):
            self._set_state("disconnected")
            self._worker = None
            self._btn_disconnect.hide()

    # ── internal ───────────────────────────────────────────────────────
    def _set_state(self, state: str, extra: str = ""):
        prev_state = self._state
        self._state = state
        colour = self._C.get(state, self._C["idle"])
        label  = _LABELS.get(state, state)
        if state == "error" and extra:
            label = f"Error: {extra[:55]}"

        self._lbl_state.setText(label)
        self._lbl_state.setStyleSheet(f"color:{colour};background:transparent;")

        if state != "streaming":
            if prev_state == "streaming":
                self._video.clear()
            self._overlay.set_state(state, label)
            self._overlay.show()
        else:
            self._overlay.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._overlay.resize(self._video.size())
        self._overlay.move(0, 0)