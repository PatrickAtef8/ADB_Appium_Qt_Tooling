import csv
import os
import subprocess
import threading
import time
import traceback
import random
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore    import Qt, QThread, QTime, QTimer, pyqtSignal, QObject
from PyQt6.QtGui     import QFont, QColor, QIcon, QPixmap, QImageReader
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPushButton, QSizePolicy, QTableWidgetItem,
    QVBoxLayout, QWidget, QRadioButton, QButtonGroup
)

from qfluentwidgets import (
    CardWidget, CaptionLabel, CheckBox, ComboBox, DoubleSpinBox,
    FluentIcon as FIF, FluentWindow, InfoBar, isDarkTheme,
    LineEdit, NavigationItemPosition, ProgressBar, PrimaryPushButton,
    PushButton, ScrollArea, setTheme, setThemeColor, SpinBox,
    StrongBodyLabel, TableWidget, TextEdit, Theme, TimeEdit,
    TitleLabel, TransparentToolButton,
)

from src.automation.appium_controller import (
    AppiumController,
    get_connected_devices,
    get_instagram_accounts,
    switch_instagram_account,
    start_scrcpy,
    stop_scrcpy,
)
from src.automation.appium_manager  import AppiumManager
from src.automation.scraper         import InstagramScraper
from src.mirror                     import MirrorWidget
from src.sheets.google_sheets       import SheetsClient, send_webhook
from src.utils.blacklist            import clear_blacklist, load_blacklist, save_blacklist
from src.utils.config_manager       import load_config, save_config
from src.utils.filters              import parse_keywords


# ─────────────────────────────────────────────────────────────────────────────
# DPI scaling helper
# ─────────────────────────────────────────────────────────────────────────────
# On Linux (typically 96 dpi) Qt uses point sizes at a 1:1 ratio with the
# physical screen.  On Windows, even at 96 dpi, the platform plugin applies
# an extra scaling pass that makes identical point-size fonts render ~25-33 %
# larger.  We detect the device pixel ratio at import time and compute a
# correction factor so that fonts and fixed pixel sizes look the same on both
# platforms.
#
# Reference DPR = 1.0  (standard 96-dpi Linux monitor)
# Windows 96 dpi reports DPR ≈ 1.0 but its GDI font metrics are larger,
# so we apply a small blanket correction whenever running on Windows.

import sys as _sys

def _dpi_scale() -> float:
    """Return a multiplier < 1.0 on Windows to counteract Qt's upscaling."""
    if _sys.platform == "win32":
        # Windows GDI renders fonts ~15% larger than Linux for the same pt size.
        # Subtract that overshoot so the UI looks identical to Linux.
        return 0.85
    return 1.0

def _pts(base_pt: int) -> int:
    """Scale a point size for the current platform."""
    return max(6, round(base_pt * _dpi_scale()))

def _px(base_px: int) -> int:
    """Scale a pixel dimension for the current platform."""
    return max(1, round(base_px * _dpi_scale()))


# ─────────────────────────────────────────────────────────────────────────────
# Typography helpers - Scaled for cross-platform (Windows/Linux)
# ─────────────────────────────────────────────────────────────────────────────

class T:
    @staticmethod
    def title():
        f = QFont("Inter, Segoe UI", _pts(18)); f.setWeight(QFont.Weight.Bold); return f
    @staticmethod
    def heading():
        f = QFont("Inter, Segoe UI", _pts(13)); f.setWeight(QFont.Weight.DemiBold); return f
    @staticmethod
    def body():
        f = QFont("Inter, Segoe UI", _pts(10)); return f
    @staticmethod
    def caption():
        f = QFont("Inter, Segoe UI", _pts(9)); return f
    @staticmethod
    def button():
        f = QFont("Inter, Segoe UI", _pts(10)); f.setWeight(QFont.Weight.Medium); return f
    @staticmethod
    def mono():
        f = QFont("JetBrains Mono, Consolas", _pts(9)); return f


# ─────────────────────────────────────────────────────────────────────────────
# MirrorResizeGrip  – draggable left edge for the mirror panel
# ─────────────────────────────────────────────────────────────────────────────

class MirrorResizeGrip(QWidget):
    """
    A thin vertical strip placed on the left edge of the mirror panel.
    Dragging it left/right resizes the panel.
    """
    width_changed = pyqtSignal(int)   # emits new panel width while dragging

    _GRIP_W = 8   # visible width of the grip strip

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self._GRIP_W)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._dragging   = False
        self._drag_start_x    = 0
        self._panel_w_at_drag = 0

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging        = True
            self._drag_start_x    = e.globalPosition().x()
            # container width minus the grip itself = current panel content width
            parent_w = self.parent().width() if self.parent() else (500 + self._GRIP_W)
            self._panel_w_at_drag = parent_w - self._GRIP_W
            e.accept()

    def mouseMoveEvent(self, e):
        if self._dragging:
            delta    = int(self._drag_start_x - e.globalPosition().x())
            new_w    = max(_px(260), min(_px(900), self._panel_w_at_drag + delta))
            self.width_changed.emit(new_w)
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            e.accept()

    def paintEvent(self, e):
        from PyQt6.QtGui import QPainter, QColor
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # subtle dotted handle in the centre
        cx = self._GRIP_W // 2
        dot_color = QColor("#475569")
        p.setBrush(dot_color)
        p.setPen(Qt.PenStyle.NoPen)
        h = self.height()
        for y in range(h // 2 - 24, h // 2 + 24, 8):
            p.drawEllipse(cx - 2, y, 4, 4)
        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# AccountDetectionWorker
# ─────────────────────────────────────────────────────────────────────────────

class AccountDetectionWorker(QThread):
    """
    Runs get_instagram_accounts() in a background thread so the UI never
    freezes while waiting for ADB + uiautomator to respond.
    """
    finished = pyqtSignal(int, list)
    error    = pyqtSignal(int)

    def __init__(self, row_idx: int, serial: str):
        super().__init__()
        self.row_idx = row_idx
        self.serial  = serial

    def run(self):
        try:
            from src.automation.appium_controller import get_instagram_accounts
            accounts = get_instagram_accounts(self.serial)
            self.finished.emit(self.row_idx, accounts)
        except Exception:
            self.error.emit(self.row_idx)


class AccountSwitchWorker(QThread):
    """
    Runs switch_instagram_account() in a background thread so the UI
    never freezes during the switch process.
    """
    finished = pyqtSignal(int, bool)

    def __init__(self, row_idx: int, serial: str, account_name: str):
        super().__init__()
        self.row_idx      = row_idx
        self.serial       = serial
        self.account_name = account_name

    def run(self):
        try:
            from src.automation.appium_controller import switch_instagram_account
            success = switch_instagram_account(self.serial, self.account_name)
            self.finished.emit(self.row_idx, success)
        except Exception:
            self.finished.emit(self.row_idx, False)


# ─────────────────────────────────────────────────────────────────────────────
# SheetsAuthWorker  – runs OAuth + connect_sheet off the main thread
# ─────────────────────────────────────────────────────────────────────────────

class SheetsAuthWorker(QThread):
    """Runs Google Sheets OAuth (may open a browser) on a background thread."""
    success  = pyqtSignal(object)   # emits the authenticated SheetsClient
    failure  = pyqtSignal(str)      # emits error message

    def __init__(self, credentials_path: str, sheet_id: str, tab_name: str,
                 existing_client=None, reuse: bool = False):
        super().__init__()
        self._credentials_path = credentials_path
        self._sheet_id         = sheet_id
        self._tab_name         = tab_name
        self._existing_client  = existing_client
        self._reuse            = reuse

    def run(self):
        try:
            from src.sheets.google_sheets import SheetsClient
            if self._reuse and self._existing_client:
                self._existing_client.connect_sheet()
                self.success.emit(self._existing_client)
            else:
                client = SheetsClient(
                    credentials_path=self._credentials_path,
                    sheet_id=self._sheet_id,
                    tab_name=self._tab_name,
                )
                client.authenticate()
                client.connect_sheet()
                self.success.emit(client)
        except Exception as e:
            self.failure.emit(str(e)[:300] if str(e) else "Authentication denied or connection refused.")


class SheetsTestWorker(QThread):
    """Runs Google Sheets test-connection off the main thread (Settings page)."""
    success  = pyqtSignal(object, int)  # (SheetsClient, row_count)
    failure  = pyqtSignal(str)

    def __init__(self, credentials_path: str, sheet_id: str, tab_name: str):
        super().__init__()
        self._credentials_path = credentials_path
        self._sheet_id         = sheet_id
        self._tab_name         = tab_name

    def run(self):
        try:
            from src.sheets.google_sheets import SheetsClient
            client = SheetsClient(
                credentials_path=self._credentials_path,
                sheet_id=self._sheet_id,
                tab_name=self._tab_name,
            )
            client.authenticate()
            client.connect_sheet()
            rows = client.get_row_count()
            self.success.emit(client, rows)
        except Exception as e:
            self.failure.emit(str(e)[:300] if str(e) else "Authentication denied or connection refused.")


# ─────────────────────────────────────────────────────────────────────────────
# PhoneWorker
# ─────────────────────────────────────────────────────────────────────────────

class PhoneWorkerSignals(QObject):
    log             = pyqtSignal(str)
    account         = pyqtSignal(dict)
    progress        = pyqtSignal(int, int)
    finished        = pyqtSignal(int, int)
    error           = pyqtSignal(int, str)
    status          = pyqtSignal(int, str)
    account_switched = pyqtSignal(int, str)   # (phone_index, new_account_name)


class PhoneWorker(QThread):
    def __init__(self, phone_index: int, serial: str, appium_port: int,
                 targets: List[str], config: dict, sheets_client: SheetsClient):
        super().__init__()
        self.phone_index  = phone_index
        self.serial       = serial
        self.appium_port  = appium_port
        self.targets      = targets
        self.config       = config
        self.sheets       = sheets_client
        self.signals      = PhoneWorkerSignals()
        self._stop_flag   = False
        self._scraper     = None
        self._controller  = None

    def stop(self):
        self._stop_flag = True
        if self._scraper:
            self._scraper.stop()

    def _log(self, msg: str):
        self.signals.log.emit(f"[Phone {self.phone_index + 1}] {msg}")

    def run(self):
        cfg             = self.config
        idx             = self.phone_index
        total_collected = 0
        try:
            self.signals.status.emit(idx, "Connecting Appium…")
            self._log(f"📱 Starting Appium session on port {self.appium_port}…")

            # ── Detect accounts BEFORE Appium starts ──────────────────────────
            # get_instagram_accounts uses ADB + uiautomator independently.
            # Must run before Appium takes control to avoid session conflicts.
            self._log("🔍 Detecting Instagram accounts…")
            device_accounts = get_instagram_accounts(self.serial)
            self._log(f"✅ Accounts found: {device_accounts}")
            # get_instagram_accounts() always returns the currently active
            # account first (Instagram shows it at the top of its switcher).
            # We build a stable ordered list and track position by name so
            # the round-robin is immune to list-order changes across calls.
            # current_account  = the account that is active RIGHT NOW.
            # acc_idx          = its position in device_accounts.
            current_account = device_accounts[0] if device_accounts else ""
            acc_idx         = 0   # index of current_account in device_accounts

            self._controller = AppiumController(
                host=cfg["appium"]["host"], port=self.appium_port
            )
            self._controller.start_session(self.serial)
            self._log("✅ Appium session started.")
            self.signals.status.emit(idx, "Running")

            blacklist   = load_blacklist()
            for u in cfg.get("blacklist", []):
                blacklist.add(u.lower())
            webhook_url = cfg.get("webhook_url", "").strip()

            def on_account(acc):
                if self._stop_flag:
                    return
                saved = self.sheets.append_account(acc)
                if saved:
                    self.signals.account.emit(acc)
                    if webhook_url:
                        threading.Thread(
                            target=send_webhook, args=(webhook_url, acc), daemon=True
                        ).start()
                else:
                    self._log(f"⚠️ Duplicate skipped: @{acc['username']}")

            delays         = cfg["delays"]
            filters        = cfg["filters"]
            mode           = cfg.get("last_mode", "followers")
            max_per_target = int(cfg.get("last_count", 100))
            schedule       = cfg.get("schedule", {})

            switch_mode    = delays.get("switch_mode", "profiles")   # "profiles" | "hours"
            switch_every   = int(delays.get("session_break_every", 100))   # used when mode=profiles
            switch_hours   = float(delays.get("switch_hours", 1))          # used when mode=hours

            since_last_switch  = 0                    # profile counter (mode=profiles)
            last_switch_time   = time.time()          # wall-clock anchor (mode=hours)

            def _check_and_switch(collected_so_far: int):
                """
                Called by the scraper after every collected profile.
                Supports two switch modes:
                  • profiles — switch after N collected profiles (original behaviour)
                  • hours    — switch after N hours of wall-clock time

                The switching mechanics are identical in both cases:
                  1. Round-robin to the next account.
                  2. Appium dismisses the list and navigates to Home.
                  3. Appium session is released so ADB has the accessibility lock.
                  4. switch_instagram_account() does the pure-ADB switch.
                  5. Appium session is reattached for continued scraping.
                """
                nonlocal since_last_switch, last_switch_time, acc_idx, current_account

                # ── Working-hours end-time enforcement ───────────────────────
                # This check runs after every collected profile so scraping
                # stops as soon as the end time is reached, not just between targets.
                if schedule.get("enabled") and self._is_past_schedule_end(schedule):
                    end = dtime(schedule["end_hour"], schedule["end_minute"])
                    self._log(f"⏰ Working hours ended ({end:%H:%M}). Stopping scraping…")
                    self._stop_flag = True
                    if self._scraper:
                        self._scraper.stop()
                    return

                if len(device_accounts) <= 1 or self._stop_flag:
                    since_last_switch += 1
                    return

                since_last_switch += 1

                # ── Decide whether it's time to switch ───────────────────────
                if switch_mode == "hours":
                    elapsed_hours = (time.time() - last_switch_time) / 3600.0
                    should_switch = (switch_hours > 0 and elapsed_hours >= switch_hours)
                else:
                    should_switch = (switch_every > 0 and since_last_switch >= switch_every)

                if not should_switch:
                    return

                # ── Strict round-robin: advance by 1 position ────────────────
                next_idx       = (acc_idx + 1) % len(device_accounts)
                target_account = device_accounts[next_idx]

                if switch_mode == "hours":
                    elapsed_str = f"{(time.time() - last_switch_time) / 3600:.1f}h"
                    self._log(
                        f"🔄 Auto-switching from [{current_account}] (idx={acc_idx}) "
                        f"→ [{target_account}] (idx={next_idx}) "
                        f"after {elapsed_str}…"
                    )
                else:
                    self._log(
                        f"🔄 Auto-switching from [{current_account}] (idx={acc_idx}) "
                        f"→ [{target_account}] (idx={next_idx}) "
                        f"after {since_last_switch} profiles…"
                    )

                # Use Appium to dismiss the list and go to Home feed
                if self._scraper:
                    self._scraper._appium_navigate_to_home()

                # Release UiAutomator2 lock so ADB gets clean accessibility access
                self._log("🔓 Releasing Appium session for ADB switch…")
                self._controller.release_for_adb()

                success = switch_instagram_account(
                    self.serial, target_account, current_account
                )

                # Reconnect Appium for continued scraping
                self._log("🔗 Reconnecting Appium session after switch…")
                self._controller.reattach_after_adb()

                if success:
                    acc_idx         = next_idx
                    current_account = target_account
                    self._log(f"✅ Switched to [{target_account}] (idx={acc_idx}), will reopen list…")
                else:
                    self._log(f"⚠️ Switch to [{target_account}] failed — keeping current account [{current_account}]")

                # Notify UI
                self.signals.account_switched.emit(self.phone_index, current_account)

                # Tell scraper to re-navigate before next profile
                if self._scraper:
                    self._scraper._need_reopen_list = True

                # Reset counters regardless of success
                since_last_switch = 0
                last_switch_time  = time.time()

            self._scraper = InstagramScraper(
                controller=self._controller,
                on_account_found=on_account,
                on_log=self._log,
                on_progress=lambda d, t: self.signals.progress.emit(d, t),
                on_switch_check=_check_and_switch,
            )

            for target in self.targets:
                if self._stop_flag:
                    break
                if schedule.get("enabled"):
                    self._wait_for_schedule(schedule)
                if self._stop_flag:
                    break

                self._log(f"🎯 Processing target: @{target}")
                self.signals.status.emit(idx, f"@{target}")

                count = self._scraper.run(
                    target_username=target,
                    mode=mode,
                    max_count=max_per_target,
                    filters=filters,
                    delays=delays,
                    fetch_details=True,
                    blacklist=blacklist,
                )
                total_collected += count
                self._log(f"✅ @{target} done — {count} this run, {total_collected} total")

                if self._stop_flag:
                    break

                # Rest between targets (skip for the last target)
                if (len(self.targets) > 1
                        and target != self.targets[-1]
                        and not self._stop_flag):
                    rest_s = random.randint(
                        int(delays.get("rest_min_minutes", 1)) * 60,
                        int(delays.get("rest_max_minutes", 5)) * 60,
                    )
                    self._log(f"😴 Resting {rest_s // 60}m {rest_s % 60}s before next target…")
                    self._sleep(rest_s)

            self.signals.finished.emit(idx, total_collected)

        except Exception as e:
            self.signals.error.emit(
                idx, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            )
        finally:
            if self._controller:
                self._controller.stop_session()

    def _wait_for_schedule(self, schedule: dict):
        start = dtime(schedule["start_hour"], schedule["start_minute"])
        end   = dtime(schedule["end_hour"],   schedule["end_minute"])
        while not self._stop_flag:
            now = datetime.now().time()
            if start <= now <= end:
                return
            self._log(f"⏰ Outside hours ({start:%H:%M}–{end:%H:%M}). Waiting…")
            self._sleep(60)

    def _is_past_schedule_end(self, schedule: dict) -> bool:
        """Return True if the current time is past the schedule end time."""
        end = dtime(schedule["end_hour"], schedule["end_minute"])
        return datetime.now().time() > end

    def _sleep(self, seconds: int):
        for _ in range(seconds):
            if self._stop_flag:
                return
            time.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
# Scrollable page base
# ─────────────────────────────────────────────────────────────────────────────

class PageWidget(ScrollArea):
    def __init__(self, title: str, parent=None):
        super().__init__(parent=parent)
        self.view       = QWidget(self)
        self.vBoxLayout = QVBoxLayout(self.view)
        self.vBoxLayout.setContentsMargins(50, 40, 50, 50)
        self.vBoxLayout.setSpacing(32)
        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.setObjectName(title.replace(" ", ""))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        lbl = TitleLabel(title, self)
        lbl.setFont(T.title())
        lbl.setStyleSheet(f"font-size: {_pts(22)}pt; margin-bottom: {_px(12)}px; background: transparent;")
        self.vBoxLayout.addWidget(lbl)

    def add(self, w):          self.vBoxLayout.addWidget(w)
    def add_layout(self, lay): self.vBoxLayout.addLayout(lay)
    def stretch(self):         self.vBoxLayout.addStretch(1)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard page
# ─────────────────────────────────────────────────────────────────────────────

class DashboardPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Dashboard")
        self.device_rows: List[Tuple] = []
        self.target_rows: List[TextEdit] = []
        self._build()

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        left_scroll = ScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setStyleSheet("ScrollArea{border:none;background:transparent;}")

        left_inner = QWidget()
        left_lay   = QVBoxLayout(left_inner)
        _m = 24 if _sys.platform == "win32" else 40   # outer horizontal margin
        _cs = 16 if _sys.platform == "win32" else 24  # card internal margin
        _sp = 14 if _sys.platform == "win32" else 24  # spacing between cards
        left_lay.setContentsMargins(_m, 40, _m, 40)
        left_lay.setSpacing(_sp)
        left_scroll.setWidget(left_inner)

        title_lbl = TitleLabel("Dashboard", left_inner)
        title_lbl.setFont(T.title())
        title_lbl.setStyleSheet(f"font-size: {_pts(22)}pt; margin-bottom: {_px(12)}px; background: transparent;")
        left_lay.addWidget(title_lbl)

        # ── Device rows ───────────────────────────────────────────────────
        dev_card = CardWidget(left_inner)
        dev_lay  = QVBoxLayout(dev_card)
        dev_lay.setContentsMargins(_cs, _cs, _cs, _cs)
        dev_lay.setSpacing(16 if _sys.platform == "win32" else 20)

        hdr_row = QHBoxLayout()
        h1 = StrongBodyLabel("📱 Connected Phones", dev_card)
        h1.setFont(T.heading())
        h1.setStyleSheet("background: transparent;")
        hdr_row.addWidget(h1)
        hdr_row.addStretch()
        self.btn_refresh = PushButton(FIF.SYNC, "Refresh", dev_card)
        self.btn_refresh.setMinimumHeight(_px(34))
        self.btn_refresh.setMinimumWidth(_px(115))
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        hdr_row.addWidget(self.btn_refresh)
        dev_lay.addLayout(hdr_row)

        for i in range(3):
            row = QHBoxLayout()
            row.setSpacing(10 if _sys.platform == "win32" else 16)

            lbl_num = StrongBodyLabel(f"P{i+1}", dev_card)
            lbl_num.setFont(T.body())
            lbl_num.setFixedWidth(28 if _sys.platform == "win32" else _px(40))
            lbl_num.setStyleSheet("background: transparent;")

            combo_dev = ComboBox(dev_card)
            combo_dev.setFont(T.body())
            combo_dev.setMinimumHeight(_px(36))
            combo_dev.setPlaceholderText("Select Device")
            combo_dev.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo_dev.setMaximumWidth(220 if _sys.platform == "win32" else 300)

            combo_acc = ComboBox(dev_card)
            combo_acc.setFont(T.body())
            combo_acc.setMinimumHeight(_px(36))
            combo_acc.setFixedWidth(120 if _sys.platform == "win32" else _px(160))
            combo_acc.setPlaceholderText("Accounts")

            lbl_port = CaptionLabel(f":{4723 + i}", dev_card)
            lbl_port.setFont(T.caption())
            lbl_port.setFixedWidth(48 if _sys.platform == "win32" else _px(60))
            lbl_port.setStyleSheet("background: transparent;")

            lbl_status = CaptionLabel("● idle", dev_card)
            lbl_status.setFont(T.caption())
            lbl_status.setFixedWidth(58 if _sys.platform == "win32" else _px(75))
            lbl_status.setStyleSheet("background: transparent;")

            btn_view = PushButton("👁 View", dev_card)
            btn_view.setFont(T.button())
            btn_view.setMinimumHeight(_px(34))
            btn_view.setFixedWidth(75 if _sys.platform == "win32" else _px(90))
            btn_view.setCursor(Qt.CursorShape.PointingHandCursor)

            row.addWidget(lbl_num)
            row.addWidget(combo_dev)
            row.addWidget(combo_acc)
            row.addWidget(lbl_port)
            row.addWidget(lbl_status)
            row.addWidget(btn_view)
            dev_lay.addLayout(row)
            self.device_rows.append((combo_dev, combo_acc, lbl_port, lbl_status, btn_view))

        left_lay.addWidget(dev_card)

        # ── Targets ───────────────────────────────────────────────────────
        tgt_card = CardWidget(left_inner)
        tgt_lay  = QVBoxLayout(tgt_card)
        tgt_lay.setContentsMargins(_cs, _cs, _cs, _cs)
        tgt_lay.setSpacing(12 if _sys.platform == "win32" else 16)
        lbl_tgt = StrongBodyLabel("🎯 Targets per Phone", tgt_card)
        lbl_tgt.setFont(T.heading())
        lbl_tgt.setStyleSheet("background: transparent;")
        tgt_lay.addWidget(lbl_tgt)

        targets_grid = QHBoxLayout()
        targets_grid.setSpacing(12 if _sys.platform == "win32" else 20)
        for i in range(3):
            col = QVBoxLayout()
            lbl = CaptionLabel(f"Phone {i+1}", tgt_card)
            lbl.setFont(T.caption())
            lbl.setStyleSheet("background: transparent;")
            txt = TextEdit(tgt_card)
            txt.setFont(T.body())
            txt.setPlaceholderText("username1\nusername2")
            txt.setMinimumHeight(_px(140))
            col.addWidget(lbl)
            col.addWidget(txt)
            targets_grid.addLayout(col)
            self.target_rows.append(txt)
        tgt_lay.addLayout(targets_grid)
        left_lay.addWidget(tgt_card)

        # ── Configuration & Controls ──────────────────────────────────────
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(14 if _sys.platform == "win32" else 24)

        # Mode & Count Card
        mode_card = CardWidget(left_inner)
        mode_lay = QVBoxLayout(mode_card)
        mode_lay.setContentsMargins(_cs, _cs, _cs, _cs)
        mode_lay.setSpacing(0)
        lbl_mode = StrongBodyLabel("⚙️ Run Settings", mode_card)
        lbl_mode.setFont(T.heading())
        lbl_mode.setStyleSheet("background: transparent;")
        mode_lay.addWidget(lbl_mode)
        mode_lay.addSpacing(16)

        mode_form = QHBoxLayout()
        mode_form.setSpacing(0)
        lbl_m = CaptionLabel("Mode:", mode_card)
        lbl_m.setFont(T.body())
        lbl_m.setStyleSheet("background: transparent;")
        mode_form.addWidget(lbl_m)
        mode_form.addSpacing(6)
        self.combo_mode = ComboBox(mode_card)
        self.combo_mode.setFont(T.body())
        self.combo_mode.setMinimumHeight(_px(34))
        self.combo_mode.addItems(["followers", "following"])
        mode_form.addWidget(self.combo_mode)
        mode_form.addSpacing(40)
        lbl_mx = CaptionLabel("Max:", mode_card)
        lbl_mx.setFont(T.body())
        lbl_mx.setStyleSheet("background: transparent;")
        mode_form.addWidget(lbl_mx)
        mode_form.addSpacing(6)
        self.spin_count = SpinBox(mode_card)
        self.spin_count.setFont(T.body())
        self.spin_count.setMinimumHeight(_px(34))
        self.spin_count.setRange(1, 50000); self.spin_count.setValue(100)
        mode_form.addWidget(self.spin_count)
        mode_form.addStretch(1)
        mode_lay.addLayout(mode_form)
        mode_lay.addStretch(1)
        bottom_row.addWidget(mode_card, 1)

        # Schedule Card
        sched_card = CardWidget(left_inner)
        sched_lay = QVBoxLayout(sched_card)
        sched_lay.setContentsMargins(_cs, _cs, _cs, _cs)
        sched_lay.setSpacing(10)
        self.chk_schedule = CheckBox("Working Hours", sched_card)
        self.chk_schedule.setFont(T.heading())
        self.chk_schedule.setStyleSheet("background: transparent;")
        sched_lay.addWidget(self.chk_schedule)
        self.lbl_sched_desc = CaptionLabel(
            "Scraping only runs between these times. Outside this window the bot pauses and waits.",
            sched_card,
        )
        self.lbl_sched_desc.setStyleSheet("background: transparent; color: grey;")
        self.lbl_sched_desc.setWordWrap(True)
        sched_lay.addWidget(self.lbl_sched_desc)
        time_row = QHBoxLayout()
        time_row.setSpacing(8)
        self._lbl_sched_start = CaptionLabel("Start:", sched_card)
        self._lbl_sched_start.setStyleSheet("background: transparent;")
        self.time_start = TimeEdit(sched_card)
        self.time_start.setFont(T.body())
        self.time_start.setMinimumHeight(_px(34))
        self.time_start.setDisplayFormat("hh:mm AP")
        self.time_start.setToolTip("Scraping START time (e.g. 09:00 AM)")
        self._lbl_sched_arrow = CaptionLabel("to", sched_card)
        self._lbl_sched_arrow.setStyleSheet("background: transparent;")
        self._lbl_sched_end = CaptionLabel("End:", sched_card)
        self._lbl_sched_end.setStyleSheet("background: transparent;")
        self.time_end = TimeEdit(sched_card)
        self.time_end.setFont(T.body())
        self.time_end.setMinimumHeight(_px(34))
        self.time_end.setDisplayFormat("hh:mm AP")
        self.time_end.setToolTip("Scraping END time (e.g. 06:00 PM)")
        time_row.addWidget(self._lbl_sched_start)
        time_row.addWidget(self.time_start)
        time_row.addWidget(self._lbl_sched_arrow)
        time_row.addWidget(self._lbl_sched_end)
        time_row.addWidget(self.time_end)
        time_row.addStretch()
        sched_lay.addLayout(time_row)
        bottom_row.addWidget(sched_card, 1)

        left_lay.addLayout(bottom_row)

        # ── Action Buttons ────────────────────────────────────────────────
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(20)
        self.btn_start = PrimaryPushButton(FIF.PLAY, "START SCRAPING", left_inner)
        self.btn_start.setFont(T.button())
        self.btn_start.setMinimumHeight(_px(48))
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setEnabled(False)   # locked until device assigned + accounts detected

        self.btn_stop = PushButton(FIF.CLOSE, "STOP ALL", left_inner)
        self.btn_stop.setFont(T.button())
        self.btn_stop.setMinimumHeight(_px(48))
        self.btn_stop.setEnabled(False)
        self.btn_stop.setObjectName("btn_stop_danger")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)

        self.lbl_overall_status = StrongBodyLabel("Ready", left_inner)
        self.lbl_overall_status.setFont(T.body())
        self.lbl_overall_status.setStyleSheet("color: #64748b; margin-left: 15px; background: transparent;")

        ctrl_row.addWidget(self.btn_start, 2)
        ctrl_row.addWidget(self.btn_stop, 1)
        ctrl_row.addWidget(self.lbl_overall_status, 1)
        left_lay.addLayout(ctrl_row)

        left_lay.addStretch(1)
        outer.addWidget(left_scroll, stretch=65)
        outer.addStretch(35)


# ─────────────────────────────────────────────────────────────────────────────
# Filters page
# ─────────────────────────────────────────────────────────────────────────────

class FiltersPage(PageWidget):
    def __init__(self, parent=None):
        super().__init__("Filters & Blacklist", parent)
        self._build()

    def _build(self):
        # Skip conditions
        skip_card = CardWidget(self)
        skip_lay  = QVBoxLayout(skip_card)
        skip_lay.setContentsMargins(30, 30, 30, 30)
        skip_lay.setSpacing(16)
        lbl_skip = StrongBodyLabel("🚫 Skip Conditions", skip_card)
        lbl_skip.setFont(T.heading())
        lbl_skip.setStyleSheet("background: transparent;")
        skip_lay.addWidget(lbl_skip)

        conditions_grid = QHBoxLayout()
        col1 = QVBoxLayout(); col2 = QVBoxLayout()
        self.chk_skip_no_bio     = CheckBox("No bio", skip_card)
        self.chk_skip_private    = CheckBox("Private account", skip_card)
        self.chk_skip_no_pic     = CheckBox("No profile picture", skip_card)
        self.chk_skip_no_contact = CheckBox(
            "No email AND no phone in bio/contact (recommended)", skip_card
        )
        for c in [self.chk_skip_no_bio, self.chk_skip_private,
                  self.chk_skip_no_pic, self.chk_skip_no_contact]:
            c.setFont(T.body()); c.setStyleSheet("background: transparent;")
        self.chk_skip_no_contact.setChecked(True)
        col1.addWidget(self.chk_skip_no_bio); col1.addWidget(self.chk_skip_private)
        col2.addWidget(self.chk_skip_no_pic); col2.addWidget(self.chk_skip_no_contact)
        conditions_grid.addLayout(col1); conditions_grid.addLayout(col2)
        skip_lay.addLayout(conditions_grid)

        row_p = QHBoxLayout()
        self.spin_min_posts = SpinBox(skip_card)
        self.spin_min_posts.setRange(0, 10000)
        self.spin_min_posts.setFont(T.body()); self.spin_min_posts.setMinimumHeight(_px(34)); self.spin_min_posts.setFixedWidth(_px(150))
        lbl_mp = CaptionLabel("Min posts:", skip_card); lbl_mp.setFont(T.body()); lbl_mp.setStyleSheet("background: transparent;")
        row_p.addWidget(lbl_mp); row_p.addWidget(self.spin_min_posts)
        row_p.addSpacing(40)

        self.chk_enable_post_spin = CheckBox("Enable post-spin (check latest post date)", skip_card)
        self.chk_enable_post_spin.setFont(T.body()); self.chk_enable_post_spin.setStyleSheet("background: transparent;")
        
        self.spin_skip_months = SpinBox(skip_card)
        self.spin_skip_months.setRange(1, 120)
        self.spin_skip_months.setFont(T.body()); self.spin_skip_months.setMinimumHeight(_px(34)); self.spin_skip_months.setFixedWidth(_px(150))
        self.spin_skip_months.setEnabled(False)
        
        lbl_sm = CaptionLabel("Months threshold:", skip_card); lbl_sm.setFont(T.body()); lbl_sm.setStyleSheet("background: transparent;")
        
        self.chk_enable_post_spin.stateChanged.connect(lambda s: self.spin_skip_months.setEnabled(s == 2))
        
        row_p.addWidget(self.chk_enable_post_spin)
        row_p.addSpacing(20)
        row_p.addWidget(lbl_sm)
        row_p.addWidget(self.spin_skip_months)

        row_p.addStretch()
        skip_lay.addLayout(row_p)
        self.add(skip_card)

        # Keyword filtering
        kw_card = CardWidget(self)
        kw_lay  = QVBoxLayout(kw_card)
        kw_lay.setContentsMargins(30, 30, 30, 30)
        kw_lay.setSpacing(16)
        lbl_kw = StrongBodyLabel("🔍 Keyword Filtering", kw_card)
        lbl_kw.setFont(T.heading()); lbl_kw.setStyleSheet("background: transparent;")
        kw_lay.addWidget(lbl_kw)

        kw_form = QHBoxLayout()
        col_skip = QVBoxLayout()
        lbl_sk = CaptionLabel("Skip if Bio/Username contains (comma-separated):", kw_card)
        lbl_sk.setFont(T.caption()); lbl_sk.setStyleSheet("background: transparent;")
        col_skip.addWidget(lbl_sk)
        self.txt_skip_keywords = TextEdit(kw_card)
        self.txt_skip_keywords.setFont(T.body())
        self.txt_skip_keywords.setPlaceholderText("crypto, scam, bot, test")
        self.txt_skip_keywords.setMinimumHeight(_px(140))
        col_skip.addWidget(self.txt_skip_keywords)

        col_only = QVBoxLayout()
        lbl_on = CaptionLabel("ONLY include profiles containing these (blank = allow all):", kw_card)
        lbl_on.setFont(T.caption()); lbl_on.setStyleSheet("background: transparent;")
        col_only.addWidget(lbl_on)
        self.txt_only_keywords = TextEdit(kw_card)
        self.txt_only_keywords.setFont(T.body())
        self.txt_only_keywords.setPlaceholderText("fitness, coach, realestate")
        self.txt_only_keywords.setMinimumHeight(_px(140))
        col_only.addWidget(self.txt_only_keywords)

        kw_form.addLayout(col_skip); kw_form.addLayout(col_only)
        kw_lay.addLayout(kw_form)
        self.add(kw_card)

        # Blacklist
        bl_card = CardWidget(self)
        bl_lay  = QVBoxLayout(bl_card)
        bl_lay.setContentsMargins(30, 30, 30, 30)
        bl_lay.setSpacing(16)
        lbl_bl = StrongBodyLabel("🏴 Blacklist", bl_card)
        lbl_bl.setFont(T.heading()); lbl_bl.setStyleSheet("background: transparent;")
        bl_lay.addWidget(lbl_bl)
        bl_lay.addWidget(CaptionLabel(
            "Usernames in this list will NEVER be scraped again. "
            "Download the .txt file to edit it, then import it back.", bl_card
        ))

        # Count label — updated dynamically by MainWindow
        self.lbl_bl_count = CaptionLabel("0 entries in blacklist", bl_card)
        self.lbl_bl_count.setStyleSheet("background: transparent; color: grey;")
        self.lbl_bl_count.setFont(T.mono())
        bl_lay.addWidget(self.lbl_bl_count)

        bl_btns = QHBoxLayout()
        self.btn_download_bl = PrimaryPushButton(FIF.DOWNLOAD, "Download .txt", bl_card)
        self.btn_import_bl   = PushButton(FIF.FOLDER,          "Import .txt",   bl_card)
        self.btn_clear_bl    = PushButton(FIF.DELETE,          "Clear All",     bl_card)
        for b in [self.btn_download_bl, self.btn_import_bl, self.btn_clear_bl]:
            b.setFont(T.button()); b.setMinimumHeight(_px(36)); bl_btns.addWidget(b)
        bl_btns.addStretch()
        bl_lay.addLayout(bl_btns)
        self.add(bl_card)
        self.stretch()


# ─────────────────────────────────────────────────────────────────────────────
# Results page
# ─────────────────────────────────────────────────────────────────────────────

class ResultsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Results")
        self.phone_status_labels: List[CaptionLabel] = []
        self._build()

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        left_scroll = ScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setStyleSheet("ScrollArea{border:none;background:transparent;}")

        left_inner = QWidget()
        left_lay   = QVBoxLayout(left_inner)
        left_lay.setContentsMargins(40, 40, 30, 40)
        left_lay.setSpacing(24)
        left_scroll.setWidget(left_inner)

        title_lbl = TitleLabel("Results & Logs", left_inner)
        title_lbl.setFont(T.title())
        title_lbl.setStyleSheet(f"font-size: {_pts(22)}pt; margin-bottom: {_px(12)}px; background: transparent;")
        left_lay.addWidget(title_lbl)

        # Status & Progress Card
        prog_card = CardWidget(left_inner)
        p_lay = QVBoxLayout(prog_card)
        p_lay.setContentsMargins(30, 30, 30, 30)
        p_lay.setSpacing(20)

        status_row = QHBoxLayout()
        lbl_live = StrongBodyLabel("📊 Phone Status:", prog_card)
        lbl_live.setFont(T.heading()); lbl_live.setStyleSheet("background: transparent;")
        status_row.addWidget(lbl_live)
        for i in range(3):
            lbl = CaptionLabel(f"Phone {i+1}: idle", prog_card)
            lbl.setFont(T.body())
            lbl.setStyleSheet("color: #64748b; font-weight: 500; background: transparent;")
            self.phone_status_labels.append(lbl)
            status_row.addWidget(lbl)
        status_row.addStretch()
        p_lay.addLayout(status_row)

        self.progress_bar = ProgressBar(prog_card)
        self.progress_bar.setValue(0); self.progress_bar.setMinimumHeight(_px(14))
        p_lay.addWidget(self.progress_bar)
        self.lbl_progress = CaptionLabel("Ready", prog_card)
        self.lbl_progress.setFont(T.body()); self.lbl_progress.setStyleSheet("background: transparent;")
        p_lay.addWidget(self.lbl_progress)
        left_lay.addWidget(prog_card)

        # Results table
        lbl_coll = StrongBodyLabel("📋 Collected Accounts", left_inner)
        lbl_coll.setFont(T.heading()); lbl_coll.setStyleSheet("background: transparent;")
        left_lay.addWidget(lbl_coll)
        self.table = TableWidget(left_inner)
        self.table.setFont(T.body())
        self.table.setColumnCount(12)
        self.table.setHorizontalHeaderLabels([
            "Username", "Full Name", "Email", "Phone", "Country",
            "Location", "Followers", "Following", "Posts", "Bio",
            "Profile URL", "Scraped At",
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(_px(400))
        left_lay.addWidget(self.table)

        exp_row = QHBoxLayout()
        self.btn_export_csv = PushButton(FIF.DOWNLOAD, "Export CSV", left_inner)
        self.btn_export_csv.setFont(T.button()); self.btn_export_csv.setMinimumHeight(_px(36))
        exp_row.addWidget(self.btn_export_csv); exp_row.addStretch()
        left_lay.addLayout(exp_row)

        # Activity log
        lbl_log = StrongBodyLabel("📜 Activity Log", left_inner)
        lbl_log.setFont(T.heading()); lbl_log.setStyleSheet("background: transparent;")
        left_lay.addWidget(lbl_log)
        self.log_area = TextEdit(left_inner)
        self.log_area.setReadOnly(True); self.log_area.setMinimumHeight(_px(250)); self.log_area.setFont(T.mono())
        left_lay.addWidget(self.log_area)

        left_lay.addStretch(1)
        outer.addWidget(left_scroll, stretch=65)
        outer.addStretch(35)


# ─────────────────────────────────────────────────────────────────────────────
# Settings page
# ─────────────────────────────────────────────────────────────────────────────

class SettingsPage(PageWidget):
    def __init__(self, parent=None):
        super().__init__("Settings", parent)
        self._build()

    def _build(self):
        # Google Sheets
        sh_card = CardWidget(self)
        sh_lay  = QVBoxLayout(sh_card)
        sh_lay.setContentsMargins(30, 30, 30, 30)
        sh_lay.setSpacing(20)
        lbl_sh = StrongBodyLabel("📊 Google Sheets Integration", sh_card)
        lbl_sh.setFont(T.heading()); lbl_sh.setStyleSheet("background: transparent;")
        sh_lay.addWidget(lbl_sh)

        def _row(label, widget):
            r = QHBoxLayout()
            lbl = CaptionLabel(label, sh_card)
            lbl.setFixedWidth(_px(140)); lbl.setFont(T.body()); lbl.setStyleSheet("background: transparent;")
            r.addWidget(lbl); r.addWidget(widget)
            sh_lay.addLayout(r)

        self.inp_sheet_id  = LineEdit(sh_card); self.inp_sheet_id.setFont(T.body()); self.inp_sheet_id.setMinimumHeight(_px(36))
        self.inp_sheet_id.setPlaceholderText("Spreadsheet ID from URL")
        self.inp_sheet_tab = LineEdit(sh_card); self.inp_sheet_tab.setFont(T.body()); self.inp_sheet_tab.setMinimumHeight(_px(36))
        self.inp_creds     = LineEdit(sh_card); self.inp_creds.setFont(T.body()); self.inp_creds.setMinimumHeight(_px(36))
        _row("Sheet ID:",  self.inp_sheet_id)
        _row("Tab Name:",  self.inp_sheet_tab)

        creds_row = QHBoxLayout()
        creds_lbl = CaptionLabel("Credentials:", sh_card)
        creds_lbl.setFixedWidth(_px(140)); creds_lbl.setFont(T.body()); creds_lbl.setStyleSheet("background: transparent;")
        self.btn_browse_creds = PushButton(FIF.FOLDER, "Browse", sh_card)
        self.btn_browse_creds.setFont(T.button()); self.btn_browse_creds.setMinimumHeight(_px(36))
        creds_row.addWidget(creds_lbl); creds_row.addWidget(self.inp_creds); creds_row.addWidget(self.btn_browse_creds)
        sh_lay.addLayout(creds_row)

        btns_row = QHBoxLayout()
        self.btn_test_sheets  = PrimaryPushButton(FIF.SEND,   "Connect & Auth", sh_card)
        self.btn_revoke_token = PushButton(FIF.DELETE,        "Revoke Token",   sh_card)
        self.lbl_sheet_status = CaptionLabel("Not connected", sh_card)
        for b in [self.btn_test_sheets, self.btn_revoke_token]:
            b.setFont(T.button()); b.setMinimumHeight(_px(36)); btns_row.addWidget(b)
        self.lbl_sheet_status.setFont(T.body()); self.lbl_sheet_status.setStyleSheet("background: transparent;")
        btns_row.addWidget(self.lbl_sheet_status); btns_row.addStretch()
        sh_lay.addLayout(btns_row)
        self.add(sh_card)

        # Webhook & Appium side by side
        wa_row = QHBoxLayout(); wa_row.setSpacing(24)

        wh_card = CardWidget(self); wh_lay = QVBoxLayout(wh_card)
        wh_lay.setContentsMargins(30, 30, 30, 30); wh_lay.setSpacing(16)
        lbl_wh = StrongBodyLabel("🔗 Webhook", wh_card)
        lbl_wh.setFont(T.heading()); lbl_wh.setStyleSheet("background: transparent;")
        wh_lay.addWidget(lbl_wh)
        wh_lay.addWidget(CaptionLabel("POST each account as JSON (blank = disabled).", wh_card))
        self.inp_webhook = LineEdit(wh_card); self.inp_webhook.setFont(T.body())
        self.inp_webhook.setMinimumHeight(_px(36)); self.inp_webhook.setPlaceholderText("https://hooks.zapier.com/…")
        wh_lay.addWidget(self.inp_webhook); wa_row.addWidget(wh_card, 1)

        ap_card = CardWidget(self); ap_lay = QVBoxLayout(ap_card)
        ap_lay.setContentsMargins(30, 30, 30, 30); ap_lay.setSpacing(16)
        lbl_ap = StrongBodyLabel("⚙️ Appium", ap_card)
        lbl_ap.setFont(T.heading()); lbl_ap.setStyleSheet("background: transparent;")
        ap_lay.addWidget(lbl_ap)
        ap_lay.addWidget(CaptionLabel(
            "Ports: Phone 1=4723, Phone 2=4724, Phone 3=4725. "
            "Change host only for remote Appium.", ap_card
        ))
        self.inp_appium_host = LineEdit(ap_card); self.inp_appium_host.setFont(T.body())
        self.inp_appium_host.setMinimumHeight(_px(36))
        ap_lay.addWidget(self.inp_appium_host); wa_row.addWidget(ap_card, 1)
        self.add_layout(wa_row)

        # Delays
        dl_card = CardWidget(self); dl_lay = QVBoxLayout(dl_card)
        dl_lay.setContentsMargins(30, 30, 30, 30); dl_lay.setSpacing(20)
        lbl_dl = StrongBodyLabel("⏱️ Randomized Delays", dl_card)
        lbl_dl.setFont(T.heading()); lbl_dl.setStyleSheet("background: transparent;")
        dl_lay.addWidget(lbl_dl)
        dl_lay.addWidget(CaptionLabel("All delays randomized between MIN and MAX.", dl_card))

        def add_delay(label, attr_min, attr_max, mn, mx, is_int=False):
            r = QHBoxLayout()
            lbl = CaptionLabel(label, dl_card); lbl.setFont(T.body())
            lbl.setStyleSheet("background: transparent;"); r.addWidget(lbl)
            if is_int:
                wmin = SpinBox(dl_card); wmax = SpinBox(dl_card)
                wmin.setRange(int(mn), int(mx)); wmax.setRange(int(mn), int(mx))
            else:
                wmin = DoubleSpinBox(dl_card); wmax = DoubleSpinBox(dl_card)
                wmin.setRange(mn, mx); wmax.setRange(mn, mx)
                wmin.setDecimals(1); wmin.setSingleStep(0.5)
                wmax.setDecimals(1); wmax.setSingleStep(0.5)
            for w in (wmin, wmax):
                w.setFont(T.body()); w.setMinimumHeight(_px(34)); w.setFixedWidth(_px(140))
                w.setKeyboardTracking(True)
            lbl_min = CaptionLabel("MIN", dl_card); lbl_min.setFont(T.caption())
            lbl_min.setStyleSheet("background: transparent;"); r.addWidget(lbl_min); r.addWidget(wmin)
            r.addSpacing(15)
            lbl_max = CaptionLabel("MAX", dl_card); lbl_max.setFont(T.caption())
            lbl_max.setStyleSheet("background: transparent;"); r.addWidget(lbl_max); r.addWidget(wmax)
            r.addStretch()
            dl_lay.addLayout(r)
            setattr(self, attr_min, wmin); setattr(self, attr_max, wmax)

        add_delay("Between profiles (s):", "sp_prof_min", "sp_prof_max", 1.0, 30.0)
        add_delay("Between scrolls (s):",  "sp_scrl_min", "sp_scrl_max", 0.5, 15.0)
        add_delay("Rest between runs (m):", "sp_rest_min", "sp_rest_max", 1, 1440, True)

        # ── Account switch mode ───────────────────────────────────────────
        lbl_sw = StrongBodyLabel("🔄 Account Switching", dl_card)
        lbl_sw.setFont(T.heading()); lbl_sw.setStyleSheet("background: transparent;")
        dl_lay.addWidget(lbl_sw)

        sm_row = QHBoxLayout()
        self.rb_switch_profiles = QRadioButton("Every", dl_card)
        self.rb_switch_profiles.setFont(T.body())
        self.rb_switch_profiles.setStyleSheet("background: transparent;")
        self.rb_switch_profiles.setChecked(True)
        self.sp_switch_every = SpinBox(dl_card)
        self.sp_switch_every.setFont(T.body()); self.sp_switch_every.setRange(1, 10000)
        self.sp_switch_every.setValue(50); self.sp_switch_every.setFixedWidth(_px(130))
        self.sp_switch_every.setMinimumHeight(_px(34)); self.sp_switch_every.setKeyboardTracking(True)

        self.rb_switch_hours = QRadioButton("Every", dl_card)
        self.rb_switch_hours.setFont(T.body())
        self.rb_switch_hours.setStyleSheet("background: transparent;")
        self.sp_switch_hours = SpinBox(dl_card)
        self.sp_switch_hours.setFont(T.body()); self.sp_switch_hours.setRange(1, 168)
        self.sp_switch_hours.setValue(1); self.sp_switch_hours.setFixedWidth(_px(130))
        self.sp_switch_hours.setMinimumHeight(_px(34)); self.sp_switch_hours.setKeyboardTracking(True)

        sm_row.addWidget(self.rb_switch_profiles); sm_row.addWidget(self.sp_switch_every)
        sm_row.addWidget(CaptionLabel("profiles", dl_card))
        sm_row.addSpacing(40)
        sm_row.addWidget(self.rb_switch_hours); sm_row.addWidget(self.sp_switch_hours)
        sm_row.addWidget(CaptionLabel("hours", dl_card))
        sm_row.addStretch()
        dl_lay.addLayout(sm_row)

        # Group the two radios so only one can be active
        self._switch_mode_group = QButtonGroup(dl_card)
        self._switch_mode_group.addButton(self.rb_switch_profiles, 0)
        self._switch_mode_group.addButton(self.rb_switch_hours,    1)

        # Grey-out the inactive spinbox whenever selection changes
        def _on_switch_mode_changed():
            by_profiles = self.rb_switch_profiles.isChecked()
            self.sp_switch_every.setEnabled(by_profiles)
            self.sp_switch_hours.setEnabled(not by_profiles)
        self._switch_mode_group.buttonClicked.connect(lambda _: _on_switch_mode_changed())
        _on_switch_mode_changed()   # apply initial state

        self.add(dl_card)
        self.stretch()


# ─────────────────────────────────────────────────────────────────────────────
# MainWindow
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()

        # ====================== ADD LOGO TO TITLE BAR ======================
        # Load the icon directly from the resource path rather than relying on
        # app.windowIcon() which can be null in a frozen Windows EXE at the
        # point MainWindow.__init__ runs (the icon assignment in main() happens
        # before MainWindow is constructed, but QFluentWindow may reset it).
        #
        # We use QImageReader for PNG (same cross-platform fix as the splash
        # screen — QIcon.pixmap() can silently return null on Windows before a
        # native window handle exists). ICO is handled by QIcon's built-in
        # decoder which does not need a window handle.
        import sys as _sys
        _base = getattr(_sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

        # Probe PNG first (preferred, crisp), then ICO as fallback.
        # Candidates walk from _MEIPASS root -> two levels up (dev layout).
        _titlebar_pixmap = QPixmap()
        for _ext, _fname in [(".png", "cansa_icon.png"), (".ico", "cansa_icon.ico")]:
            for _rel in [
                os.path.join(_base, _fname),
                os.path.join(_base, "..", "..", _fname),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", _fname),
            ]:
                _path = os.path.normpath(_rel)
                if not os.path.exists(_path):
                    continue
                if _ext == ".png":
                    _reader = QImageReader(_path)
                    _reader.setAutoTransform(True)
                    _img = _reader.read()
                    if not _img.isNull():
                        _titlebar_pixmap = QPixmap.fromImage(_img).scaled(
                            32, 32,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        break
                else:  # ICO — built-in decoder, QIcon is fine
                    _icon_tmp = QIcon(_path)
                    if not _icon_tmp.isNull():
                        _titlebar_pixmap = _icon_tmp.pixmap(32, 32)
                        break
            if not _titlebar_pixmap.isNull():
                break

        if _titlebar_pixmap.isNull():
            # Last resort: use whatever the app-level window icon is
            _app_icon = QApplication.instance().windowIcon()
            if not _app_icon.isNull():
                _titlebar_pixmap = _app_icon.pixmap(32, 32)

        if not _titlebar_pixmap.isNull():
            logo_label = QLabel(self.titleBar)
            logo_label.setPixmap(_titlebar_pixmap)
            logo_label.setFixedSize(_px(40), _px(40))
            logo_label.setContentsMargins(10, 0, 0, 0)
            logo_label.setStyleSheet("background: transparent;")
            self.titleBar.hBoxLayout.insertWidget(
                0, logo_label, 0,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
        # =================================================================

        self.cfg                = load_config()
        self._workers:          List[PhoneWorker]      = []
        self._collected         = 0
        self._active_phones     = 0
        self._done_phones       = 0
        self._mirror_phone_idx: Optional[int]          = None
        self._scrcpy_procs:     Dict[str, subprocess.Popen] = {}
        self._appium_mgr        = AppiumManager()
        self._sheets_client:    Optional[SheetsClient] = None
        self._detection_workers: Dict[int, AccountDetectionWorker] = {}
        self._switch_workers:    Dict[int, AccountSwitchWorker]    = {}

        self.setWindowTitle("Cansa")
        self.resize(1600, 1000)

        self.dashboard_page = DashboardPage(self)
        self.filters_page   = FiltersPage(self)
        self.results_page   = ResultsPage(self)
        self.settings_page  = SettingsPage(self)

        self._init_persistent_mirror()
        self._init_nav()
        self._init_theme()
        self._load_cfg_into_ui()
        self._connect_signals()
        self._refresh_devices()
        self._reload_blacklist_ui()
        self._on_schedule_toggled()   # apply enabled/disabled state on load

    # ── Persistent mirror panel ───────────────────────────────────────────
    def _init_persistent_mirror(self):
        # Restore saved width (default 500, clamp to valid range)
        self._mirror_width: int = max(_px(260), min(_px(900), int(self.cfg.get("mirror_width", _px(500)))))

        # ── Outer container: grip + panel side by side ────────────────────
        self._mirror_container = QWidget(self)
        self._mirror_container.setObjectName("MirrorContainer")
        container_lay = QHBoxLayout(self._mirror_container)
        container_lay.setContentsMargins(0, 0, 0, 0)
        container_lay.setSpacing(0)

        # Draggable left-edge grip
        self._resize_grip = MirrorResizeGrip(self._mirror_container)
        self._resize_grip.width_changed.connect(self._on_mirror_resize_drag)
        container_lay.addWidget(self._resize_grip)

        # Actual panel (everything that was there before)
        self.mirror_panel = QWidget(self._mirror_container)
        self.mirror_panel.setObjectName("PersistentMirrorPanel")
        container_lay.addWidget(self.mirror_panel, stretch=1)

        layout = QVBoxLayout(self.mirror_panel)
        layout.setContentsMargins(10, 30, 30, 30)
        layout.setSpacing(20)

        hdr_row = QHBoxLayout()
        mirror_title = StrongBodyLabel("📺 Live Mirror", self.mirror_panel)
        mirror_title.setFont(T.heading())
        mirror_title.setStyleSheet("background: transparent;")
        hdr_row.addWidget(mirror_title)
        hdr_row.addStretch()

        # Content scale state
        self._content_scale      = 1.0
        self._content_scale_prev = 1.0
        self._btn_scale_down: Optional[QPushButton] = None
        self._btn_scale_up:   Optional[QPushButton] = None

        # Width control buttons  ─  and  +
        self._btn_mirror_shrink = QPushButton("−", self.mirror_panel)
        self._btn_mirror_shrink.setFixedSize(_px(30), _px(30))
        self._btn_mirror_shrink.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mirror_shrink.setToolTip("Shrink mirror panel")
        self._btn_mirror_shrink.clicked.connect(lambda: self._step_mirror_width(-60))

        self._btn_mirror_grow = QPushButton("+", self.mirror_panel)
        self._btn_mirror_grow.setFixedSize(_px(30), _px(30))
        self._btn_mirror_grow.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_mirror_grow.setToolTip("Grow mirror panel")
        self._btn_mirror_grow.clicked.connect(lambda: self._step_mirror_width(+60))

        hdr_row.addWidget(self._btn_mirror_shrink)
        hdr_row.addWidget(self._btn_mirror_grow)
        hdr_row.addSpacing(8)

        self.lbl_mirror_device = CaptionLabel("No device selected", self.mirror_panel)
        self.lbl_mirror_device.setFont(T.body())
        self.lbl_mirror_device.setStyleSheet("background: transparent;")
        hdr_row.addWidget(self.lbl_mirror_device)
        layout.addLayout(hdr_row)

        self.mirror = MirrorWidget(phone_index=0, parent=self.mirror_panel)
        self.mirror.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.mirror.detached.connect(self._on_mirror_detached)
        layout.addWidget(self.mirror, stretch=1)
        self._mirror_container.hide()

    # ── Mirror width helpers ──────────────────────────────────────────────
    def _on_mirror_resize_drag(self, new_width: int):
        self._mirror_width = new_width
        self._reposition_mirror()

    def _step_mirror_width(self, delta: int):
        self._mirror_width = max(_px(260), min(_px(900), self._mirror_width + delta))
        self._reposition_mirror()

    def _step_content_scale(self, delta: float):
        self._content_scale_prev = self._content_scale
        self._content_scale = round(max(0.7, min(1.5, self._content_scale + delta)), 1)
        self._apply_content_scale()

    def _reposition_mirror(self):
        """Reposition the mirror container to match _mirror_width."""
        if not hasattr(self, "_mirror_container"):
            return
        grip_w = MirrorResizeGrip._GRIP_W
        total_w = self._mirror_width + grip_w
        tb_h = self.titleBar.height()
        h = self.height() - tb_h

        self._mirror_container.setGeometry(
            self.width() - total_w, tb_h, total_w, h,
        )

        # Force the mirror_panel's layout to fully re-activate so MirrorWidget
        # fills the new size.  Simply calling setGeometry on the container is
        # not enough — Qt won't re-run the layout pass on children that were
        # already laid out at a smaller size unless we explicitly tell every
        # layout in the chain to invalidate and re-activate.
        lay = self.mirror_panel.layout()
        if lay:
            lay.invalidate()
            lay.activate()

        if hasattr(self, "mirror"):
            self.mirror.updateGeometry()
            self.mirror.update()
            # MirrorWidget may render video into a manually-placed child surface
            # that doesn't respond to layout signals.  Explicitly set its geometry
            # to fill the available space inside mirror_panel so the video always
            # covers the full panel regardless of how MirrorWidget is implemented.
            mp = self.mirror_panel
            margins = mp.layout().contentsMargins()
            spacing = mp.layout().spacing()
            # Header row is the first item in the layout; measure its actual height
            header_h = 0
            lay = mp.layout()
            if lay.count() > 0:
                first_item = lay.itemAt(0)
                if first_item and first_item.layout():
                    header_h = first_item.layout().sizeHint().height() + spacing
                elif first_item and first_item.widget():
                    header_h = first_item.widget().sizeHint().height() + spacing
            target_w = mp.width()  - margins.left() - margins.right()
            target_h = mp.height() - margins.top()  - margins.bottom() - header_h
            if target_w > 10 and target_h > 10:
                self.mirror.setGeometry(margins.left(), margins.top() + header_h,
                                        target_w, target_h)

        # Save width to config immediately
        self.cfg["mirror_width"] = self._mirror_width

        # When the device screen is idle (no active touches/frames arriving),
        # the video surface never gets a new frame to trigger a repaint at the
        # new size.  Fire a one-shot timer to force a repaint after Qt has
        # finished its geometry pass — this makes resizing reliable whether
        # the screen is active or completely idle.
        QTimer.singleShot(50, self._nudge_mirror_repaint)

    def _nudge_mirror_repaint(self):
        """Force the mirror video surface to repaint at its current size."""
        if not hasattr(self, "mirror"):
            return
        self.mirror.repaint()
        # Also repaint every child widget inside MirrorWidget — the actual
        # video surface is typically a child QWidget, not MirrorWidget itself.
        for child in self.mirror.findChildren(QWidget):
            child.repaint()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reposition_mirror()

    def _update_mirror_visibility(self):
        current = self.stackedWidget.currentWidget()
        show = current in [self.dashboard_page, self.results_page]
        self._mirror_container.setVisible(show)
        if show:
            self._reposition_mirror()

    # ── Navigation ────────────────────────────────────────────────────────
    def _init_nav(self):
        self.addSubInterface(self.dashboard_page, FIF.HOME,      "Dashboard")
        self.addSubInterface(self.filters_page,   FIF.FILTER,    "Filters & Blacklist")
        self.addSubInterface(self.results_page,   FIF.COMPLETED, "Results")
        self.addSubInterface(
            self.settings_page, FIF.SETTING, "Settings",
            NavigationItemPosition.BOTTOM
        )
        self.stackedWidget.currentChanged.connect(self._update_mirror_visibility)
        self._update_mirror_visibility()

    # ── Theme ─────────────────────────────────────────────────────────────
    def _init_theme(self):
        setTheme(Theme.DARK)
        setThemeColor("#3b82f6")
        self._apply_stylesheet()
        btn = TransparentToolButton(FIF.BRUSH, self)
        btn.clicked.connect(self._toggle_theme)
        self.titleBar.hBoxLayout.insertWidget(0, btn, 0, Qt.AlignmentFlag.AlignLeft)
        if hasattr(self, "mirror"):
            self.mirror.update_theme()

        # Content scale ± buttons in title bar
        self._btn_scale_down = QPushButton("−", self)
        self._btn_scale_down.setFixedSize(26, 26)
        self._btn_scale_down.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_scale_down.setToolTip("Zoom out content")
        self._btn_scale_down.clicked.connect(lambda: self._step_content_scale(-0.1))

        self._btn_scale_up = QPushButton("+", self)
        self._btn_scale_up.setFixedSize(26, 26)
        self._btn_scale_up.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_scale_up.setToolTip("Zoom in content")
        self._btn_scale_up.clicked.connect(lambda: self._step_content_scale(+0.1))

        self.titleBar.hBoxLayout.insertWidget(1, self._btn_scale_up,   0, Qt.AlignmentFlag.AlignLeft)
        self.titleBar.hBoxLayout.insertWidget(1, self._btn_scale_down, 0, Qt.AlignmentFlag.AlignLeft)
        self._refresh_zoom_btn_styles()

    def _apply_stylesheet(self):
        dark = isDarkTheme()
        bg = "#0f172a" if dark else "#f8fafc"
        card_bg = "#1e293b" if dark else "#ffffff"
        text = "#f8fafc" if dark else "#0f172a"
        border = "#334155" if dark else "#e2e8f0"
        css = f"""
            QWidget{{ background: {bg}; color: {text}; font-family: 'Inter', 'Segoe UI'; }}
            ScrollArea{{ background: transparent; border: none; }}
            CardWidget{{ background: {card_bg}; border: 1px solid {border}; border-radius: 16px; }}
            QLabel, StrongBodyLabel, CaptionLabel, TitleLabel{{ background: transparent; }}
            LineEdit, SpinBox, DoubleSpinBox, ComboBox, TextEdit{{
                background: {bg}; border: 1px solid {border}; border-radius: 10px;
                padding: 6px 14px; font-size: 10pt;
            }}
            TimeEdit{{
                background: {bg}; border: 1px solid {border}; border-radius: 10px;
                padding: 2px 4px; font-size: 10pt;
                min-width: 88px; max-width: 110px;
            }}
            LineEdit:focus, SpinBox:focus, ComboBox:focus, TimeEdit:focus{{ border: 2px solid #3b82f6; }}
            TextEdit:focus{{ border: 2px solid #3b82f6; }}
            PushButton{{ background: {border}; border: 1px solid {border}; border-radius: 10px; padding: 6px 16px; font-weight: 500; font-size: 10pt; }}
            PushButton:hover{{ background: #475569; }}
            PushButton#btn_stop_danger{{ background: #ef4444; border: none; color: white; font-weight: bold; }}
            PushButton#btn_stop_danger:hover{{ background: #dc2626; }}
            PrimaryPushButton{{ background: #3b82f6; border: none; border-radius: 10px; color: white; font-weight: 600; padding: 6px 16px; font-size: 10pt; }}
            PrimaryPushButton:hover{{ background: #2563eb; }}
            TableWidget{{ background: {card_bg}; border: 1px solid {border}; border-radius: 10px; gridline-color: {border}; font-size: 9pt; }}
            TableWidget::item{{ padding: 4px; background: {card_bg}; color: {text}; }}
            TableWidget::item:selected{{ background: {border}; color: {text}; }}
            QHeaderView::section{{ background: {bg}; color: {text}; border: none; border-bottom: 1px solid {border}; padding: 10px; font-weight: 600; font-size: 9pt; }}
            ProgressBar{{ background: {border}; border-radius: 7px; border: none; }}
            ProgressBar::chunk{{ background: #3b82f6; border-radius: 7px; }}
            CheckBox{{ color: {text}; background: transparent; font-size: 10pt; }}
            QScrollBar:vertical{{ background: {bg}; width: 7px; border: none; }}
            QScrollBar::handle:vertical{{ background: {border}; border-radius: 3px; min-height: 16px; }}
            QComboBox QAbstractItemView{{ background: {card_bg}; color: {text}; selection-background-color: #3b82f6; border: 1px solid {border}; }}
            #PersistentMirrorPanel{{ background: {bg}; border-left: 1px solid {border}; }}
            #MirrorContainer{{ background: {bg}; }}
            MirrorResizeGrip{{ background: transparent; }}
            MirrorResizeGrip:hover{{ background: rgba(59,130,246,0.15); }}
            QRadioButton{{ background: transparent; font-size: 10pt; }}
        """
        self.setStyleSheet(css)

    def _toggle_theme(self):
        setTheme(Theme.LIGHT if isDarkTheme() else Theme.DARK)
        self._apply_stylesheet()
        if hasattr(self, "mirror"):
            self.mirror.update_theme()
        self._refresh_zoom_btn_styles()

    # ── Zoom button stylesheet (theme-aware) ──────────────────────────────
    def _zoom_btn_ss(self) -> str:
        dark = isDarkTheme()
        bg     = "#1e293b" if dark else "#e2e8f0"
        bg_hov = "#334155" if dark else "#cbd5e1"
        bg_pre = "#0f172a" if dark else "#94a3b8"
        col    = "#f8fafc"  if dark else "#0f172a"
        brd    = "#475569"  if dark else "#94a3b8"
        return (
            f"QPushButton {{background:{bg};color:{col};"
            f"border:1px solid {brd};border-radius:4px;"
            f"font-size:16px;font-weight:bold;}}"
            f"QPushButton:hover {{background:{bg_hov};}}"
            f"QPushButton:pressed {{background:{bg_pre};}}"
        )

    def _refresh_zoom_btn_styles(self):
        ss = self._zoom_btn_ss()
        for btn in (self._btn_mirror_shrink, self._btn_mirror_grow,
                    self._btn_scale_down, self._btn_scale_up):
            if btn:
                btn.setStyleSheet(ss)

    # ── Content scale (live font/size walk) ───────────────────────────────
    def _apply_content_scale(self):
        """Walk every child widget of the 4 pages and rescale fonts + heights symmetrically."""
        s = self._content_scale
        pages = [self.dashboard_page, self.filters_page,
                 self.results_page,   self.settings_page]

        # Base pt sizes at scale 1.0 (after platform correction)
        base = {
            "title":   _pts(22),
            "heading": _pts(13),
            "body":    _pts(10),
            "caption": _pts(9),
            "button":  _pts(10),
            "mono":    _pts(9),
        }

        def scaled_pt(pt):
            return max(6, round(pt * s))

        # ── Type helpers ──────────────────────────────────────────────────
        # qfluentwidgets.ComboBox  → inherits QPushButton, NOT QComboBox
        # qfluentwidgets.ScrollArea → inherits QScrollArea (must be excluded
        #   from the "text area" branch — only QTextEdit / QAbstractItemView
        #   should be treated as scalable scroll areas)
        # qfluentwidgets.TimeEdit  → sets maximumHeight=33 internally in its
        #   constructor, so we must unlock it with a large value before scaling
        from PyQt6.QtWidgets import (
            QAbstractSpinBox    as _ASB,
            QAbstractButton     as _ABT,
            QAbstractItemView   as _AIV,   # TableWidget base
            QTextEdit           as _QTE,   # TextEdit base
            QScrollArea         as _QSA,   # container ScrollAreas — EXCLUDED
            QLineEdit           as _LE,
            QProgressBar        as _PB,
            QCheckBox           as _ChB,
            QRadioButton        as _RB,
        )
        from qfluentwidgets import ComboBox as _FCombo, CaptionLabel as _CaptLbl, CheckBox as _FChB

        def _is_combo(w):
            return isinstance(w, _FCombo)

        def _is_spinbox(w):
            # SpinBox / DoubleSpinBox / TimeEdit all inherit QAbstractSpinBox
            return isinstance(w, _ASB)

        def _is_text_area(w):
            # Only real content areas: TextEdit and TableWidget.
            # Excludes QScrollArea (layout containers like left_scroll on Dashboard).
            return isinstance(w, (_QTE, _AIV)) and not isinstance(w, _QSA)

        def _is_real_button(w):
            return (isinstance(w, _ABT)
                    and not isinstance(w, (_ChB, _RB))
                    and not _is_combo(w))

        def _is_lineedit(w):
            return isinstance(w, _LE)

        def _is_progressbar(w):
            return isinstance(w, _PB)

        def _is_caption_label(w):
            return isinstance(w, _CaptLbl)

        def _is_fluent_checkbox(w):
            return isinstance(w, _FChB)

        # ── First pass: tag every widget with its true base height ────────
        # Stored once — never recalculated — so heights never drift.
        for page in pages:
            for w in page.findChildren(QWidget):
                if getattr(w, "_scale_base_h", None) is not None:
                    continue   # already tagged

                if _is_combo(w) or _is_lineedit(w):
                    w._scale_base_h = _px(34)

                elif _is_spinbox(w):
                    w._scale_base_h = _px(34)

                elif _is_real_button(w):
                    w._scale_base_h = _px(48) if w.minimumHeight() >= _px(44) else _px(36)

                elif _is_progressbar(w):
                    w._scale_base_h = _px(14)

                elif _is_text_area(w):
                    mh = w.minimumHeight()
                    if mh > 0:
                        w._scale_base_h = mh   # TextEdit 140/250 px, TableWidget 400 px

                elif _is_fluent_checkbox(w):
                    w._scale_base_h = _px(22)

                elif _is_caption_label(w):
                    w._scale_base_h = _px(16)

        # ── Second pass: apply fonts + heights ────────────────────────────
        for page in pages:
            for w in page.findChildren(QWidget):
                # Font rescaling
                f = w.font()
                pt = f.pointSize()
                if pt > 0:
                    closest_key = min(
                        base,
                        key=lambda k: abs(base[k] - round(pt / (self._content_scale_prev or 1.0)))
                    )
                    new_pt = scaled_pt(base[closest_key])
                    if f.pointSize() != new_pt:
                        f.setPointSize(new_pt)
                        w.setFont(f)

                # Height rescaling — skip untagged widgets
                base_h = getattr(w, "_scale_base_h", None)
                if base_h is None:
                    continue

                if _is_text_area(w):
                    w.setMinimumHeight(max(40, round(base_h * s)))

                elif _is_spinbox(w):
                    new_h = max(20, round(base_h * s))
                    # qfluentwidgets.TimeEdit (and some SpinBoxes) set an internal
                    # maximumHeight cap in their constructor. Unlock it first with
                    # a large value, THEN set the real target — otherwise Qt clamps
                    # setMinimumHeight to the old maximumHeight silently.
                    w.setMaximumHeight(16777215)   # Qt QWIDGETSIZE_MAX — full unlock
                    w.setMaximumHeight(new_h)
                    w.setMinimumHeight(new_h)

                elif _is_combo(w) or _is_lineedit(w):
                    w.setMinimumHeight(max(20, round(base_h * s)))

                elif _is_real_button(w):
                    w.setMinimumHeight(max(20, round(base_h * s)))

                elif _is_progressbar(w):
                    w.setMinimumHeight(max(6, round(base_h * s)))

                elif _is_fluent_checkbox(w):
                    w.setMinimumHeight(max(14, round(base_h * s)))

                elif _is_caption_label(w):
                    w.setMinimumHeight(max(10, round(base_h * s)))

        self._content_scale_prev = s

    # ── Signal connections ────────────────────────────────────────────────
    def _connect_signals(self):
        dp = self.dashboard_page
        fp = self.filters_page
        sp = self.settings_page
        rp = self.results_page

        dp.btn_refresh.clicked.connect(self._refresh_devices)
        dp.btn_start.clicked.connect(self._start_scraping)
        dp.btn_stop.clicked.connect(self._stop_all)
        dp.chk_schedule.stateChanged.connect(self._on_schedule_toggled)

        for i, (combo_dev, combo_acc, lbl_port, lbl_status, btn_view) in enumerate(dp.device_rows):
            combo_dev.currentIndexChanged.connect(
                lambda _v, idx=i: self._on_device_selected(idx)
            )
            combo_acc.currentIndexChanged.connect(
                lambda _v, idx=i: self._on_account_selected(idx)
            )
            btn_view.clicked.connect(
                lambda _checked=False, idx=i: self._on_view_clicked(idx)
            )

        sp.btn_browse_creds.clicked.connect(self._browse_credentials)
        sp.btn_test_sheets.clicked.connect(self._test_sheets)
        sp.btn_revoke_token.clicked.connect(self._revoke_token)
        fp.btn_download_bl.clicked.connect(self._download_blacklist_txt)
        fp.btn_import_bl.clicked.connect(self._import_blacklist_txt)
        fp.btn_clear_bl.clicked.connect(self._clear_blacklist)
        rp.btn_export_csv.clicked.connect(self._export_csv)

    # ── Device helpers ────────────────────────────────────────────────────
    # ── Working-hours toggle ──────────────────────────────────────────────
    def _set_schedule_locked(self, locked: bool):
        """Lock/unlock the Working Hours card while scraping is active."""
        dp = self.dashboard_page
        for w in [dp.chk_schedule, dp.time_start, dp.time_end,
                  dp._lbl_sched_start, dp._lbl_sched_end, dp._lbl_sched_arrow,
                  dp.lbl_sched_desc]:
            w.setEnabled(False if locked else (
                w is dp.chk_schedule or dp.chk_schedule.isChecked()
            ))

    def _on_schedule_toggled(self, _state=None):
        """Enable/disable time pickers based on the Working Hours checkbox."""
        dp = self.dashboard_page
        enabled = dp.chk_schedule.isChecked()
        for w in [dp.time_start, dp.time_end,
                  dp._lbl_sched_start, dp._lbl_sched_end, dp._lbl_sched_arrow,
                  dp.lbl_sched_desc]:
            w.setEnabled(enabled)

    def _refresh_devices(self):
        devices = get_connected_devices()
        dp = self.dashboard_page
        for i, (combo_dev, combo_acc, lbl_port, lbl_status, btn) in enumerate(dp.device_rows):
            combo_dev.blockSignals(True)
            combo_dev.clear()
            combo_dev.addItem("(not assigned)", userData=None)
            for serial, model in devices:
                combo_dev.addItem(f"{model} [{serial}]", userData=serial)
            combo_dev.setCurrentIndex(0)
            combo_dev.blockSignals(False)
            self._on_device_selected(i)

        if devices:
            names = ", ".join(model for _, model in devices)
            count = len(devices)
            label = "device" if count == 1 else "devices"
            InfoBar.success(
                title=f"{count} {label} connected",
                content=f"{names} — select a slot to assign.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True, duration=4000, parent=self,
            )
        else:
            InfoBar.warning(
                title="No devices found",
                content="Connect a phone via USB and press Refresh.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True, duration=4000, parent=self,
            )
        self._log(f"🔍 Found {len(devices)} device(s).")

    def _on_device_selected(self, idx: int):
        dp = self.dashboard_page
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[idx]
        serial = combo_dev.currentData()

        combo_acc.clear()
        if not serial:
            return

        old_worker = self._detection_workers.pop(idx, None)
        if old_worker and old_worker.isRunning():
            old_worker.terminate()
            old_worker.wait(500)

        combo_acc.setPlaceholderText("Detecting…")
        combo_acc.setEnabled(False)
        lbl_status.setText("⏳ detecting")

        worker = AccountDetectionWorker(row_idx=idx, serial=serial)
        worker.finished.connect(self._on_accounts_detected)
        worker.error.connect(self._on_accounts_error)
        self._detection_workers[idx] = worker
        worker.start()
        self._update_start_button_state()

    def _on_accounts_detected(self, row_idx: int, accounts: list):
        dp = self.dashboard_page
        if row_idx >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]
        combo_acc.clear()
        combo_acc.setEnabled(True)
        combo_acc.setPlaceholderText("Accounts")
        combo_acc.blockSignals(True)
        for a in accounts:
            combo_acc.addItem(a)
        combo_acc.blockSignals(False)
        lbl_status.setText("● idle")
        self._detection_workers.pop(row_idx, None)
        self._update_start_button_state()

    def _on_accounts_error(self, row_idx: int):
        dp = self.dashboard_page
        if row_idx >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]
        combo_acc.clear()
        combo_acc.setEnabled(True)
        combo_acc.setPlaceholderText("Accounts")
        combo_acc.blockSignals(True)
        combo_acc.addItem("Account 1")
        combo_acc.blockSignals(False)
        lbl_status.setText("● idle")
        self._detection_workers.pop(row_idx, None)
        self._update_start_button_state()

    def _update_start_button_state(self):
        """Enable START SCRAPING only when a device is assigned and no detection is running."""
        dp = self.dashboard_page
        any_device = False
        detecting = False
        for i, (combo_dev, combo_acc, lbl_port, lbl_status, btn_view) in enumerate(dp.device_rows):
            if combo_dev.currentData():
                any_device = True
                if i in self._detection_workers and self._detection_workers[i].isRunning():
                    detecting = True
                    break
        if not dp.btn_stop.isEnabled():  # not currently scraping
            dp.btn_start.setEnabled(any_device and not detecting)

    def _on_account_selected(self, row_idx: int):
        dp = self.dashboard_page
        if row_idx >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]
        if not combo_acc.isEnabled():
            return
        serial = combo_dev.currentData()
        if not serial:
            return
        account_index = combo_acc.currentIndex()
        if account_index < 0:
            return
        account_name = combo_acc.currentText()
        if not account_name:
            return

        old = self._switch_workers.pop(row_idx, None)
        if old and old.isRunning():
            old.terminate()
            old.wait(500)

        lbl_status.setText("⏳ switching")
        combo_acc.setEnabled(False)

        worker = AccountSwitchWorker(row_idx=row_idx, serial=serial, account_name=account_name)
        worker.finished.connect(self._on_switch_done)
        self._switch_workers[row_idx] = worker
        worker.start()

    def _on_switch_done(self, row_idx: int, success: bool):
        dp = self.dashboard_page
        if row_idx >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]
        combo_acc.setEnabled(True)
        if success:
            lbl_status.setText("● idle")
            self._log(f"✅ [Phone {row_idx + 1}] Switched to @{combo_acc.currentText()}")
        else:
            lbl_status.setText("⚠ switch failed")
            self._log(f"⚠️ [Phone {row_idx + 1}] Account switch failed")
        self._switch_workers.pop(row_idx, None)

    def _on_auto_switch(self, phone_idx: int, new_account: str):
        """
        Called (via Qt signal from PhoneWorker) whenever an automatic
        account switch completes during scraping.  Updates the combo_acc
        dropdown on the dashboard so the displayed account always matches
        the one that is actually active on the device.
        """
        dp = self.dashboard_page
        if phone_idx >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[phone_idx]

        # Find the index of the new account name in the combo box
        idx = combo_acc.findText(new_account)
        if idx >= 0:
            combo_acc.blockSignals(True)          # prevent triggering a manual switch
            combo_acc.setCurrentIndex(idx)
            combo_acc.blockSignals(False)
        else:
            # Account name not yet in list (e.g. detected after start) — add it
            combo_acc.blockSignals(True)
            combo_acc.addItem(new_account)
            combo_acc.setCurrentIndex(combo_acc.count() - 1)
            combo_acc.blockSignals(False)

        self._log(f"🔄 [Phone {phone_idx + 1}] Dashboard updated → @{new_account}")

    def _get_assigned_devices(self) -> List[Tuple[int, str]]:
        return [
            (i, combo_dev.currentData())
            for i, (combo_dev, *_) in enumerate(self.dashboard_page.device_rows)
            if combo_dev.currentData()
        ]

    def _get_phone_targets(self) -> List[List[str]]:
        return [
            [t.strip().lstrip("@") for t in txt.toPlainText().splitlines() if t.strip()]
            for txt in self.dashboard_page.target_rows
        ]

    # ── Mirror / scrcpy ───────────────────────────────────────────────────
    def _on_mirror_detached(self):
        if self._mirror_phone_idx is not None:
            try:
                btn = self.dashboard_page.device_rows[self._mirror_phone_idx][4]
                btn.setText("👁 View")
            except IndexError:
                pass
            self._mirror_phone_idx = None
            self.lbl_mirror_device.setText("No device selected")

    def _on_view_clicked(self, row_idx: int):
        dp = self.dashboard_page
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]
        serial = combo_dev.currentData()

        if not serial:
            InfoBar.warning("No Device", f"Phone {row_idx + 1} has no device assigned.", parent=self)
            return

        if self._mirror_phone_idx == row_idx:
            self.mirror.detach()
            return

        if self._mirror_phone_idx is not None:
            try:
                prev_btn = dp.device_rows[self._mirror_phone_idx][4]
                prev_btn.setText("👁 View")
            except IndexError:
                pass

        self._mirror_phone_idx = None
        self.mirror.attach(serial)
        self._mirror_phone_idx = row_idx
        btn_view.setText("⏹ Stop")
        self.lbl_mirror_device.setText(f"Mirroring: Phone {row_idx + 1}")
        self.mirror.update_phone_index(row_idx)
        self._update_mirror_visibility()

    # ── Config collect / load ─────────────────────────────────────────────
    def _collect_cfg(self) -> dict:
        cfg = load_config()
        dp  = self.dashboard_page
        fp  = self.filters_page
        sp  = self.settings_page

        cfg["devices"] = [
            {"serial": row[0].currentData()} for row in dp.device_rows
        ]
        cfg["targets_per_phone"] = [
            [t.strip().lstrip("@") for t in txt.toPlainText().splitlines() if t.strip()]
            for txt in dp.target_rows
        ]
        cfg["last_mode"]  = dp.combo_mode.currentText()
        cfg["last_count"] = dp.spin_count.value()

        ts = dp.time_start.time()
        te = dp.time_end.time()
        cfg["schedule"] = {
            "enabled":      dp.chk_schedule.isChecked(),
            "start_hour":   ts.hour(),
            "start_minute": ts.minute(),
            "end_hour":     te.hour(),
            "end_minute":   te.minute(),
        }
        cfg["filters"] = {
            "skip_no_bio":              fp.chk_skip_no_bio.isChecked(),
            "skip_private":             fp.chk_skip_private.isChecked(),
            "skip_no_profile_pic":      fp.chk_skip_no_pic.isChecked(),
            "skip_no_contact":          fp.chk_skip_no_contact.isChecked(),
            "min_posts":                fp.spin_min_posts.value(),
            "enable_post_spin":         fp.chk_enable_post_spin.isChecked(),
            "skip_no_posts_last_n_months": fp.spin_skip_months.value(),
            "keywords":                 parse_keywords(fp.txt_skip_keywords.toPlainText()),
            "only_keywords":            parse_keywords(fp.txt_only_keywords.toPlainText()),
        }
        cfg["sheet_id"]         = sp.inp_sheet_id.text().strip()
        cfg["sheet_tab"]        = sp.inp_sheet_tab.text().strip() or "Sheet1"
        cfg["credentials_path"] = sp.inp_creds.text().strip()
        cfg["webhook_url"]      = sp.inp_webhook.text().strip()
        cfg["appium"]["host"]   = sp.inp_appium_host.text().strip()
        cfg["delays"] = {
            "between_profiles_min":   sp.sp_prof_min.value(),
            "between_profiles_max":   sp.sp_prof_max.value(),
            "between_scrolls_min":    sp.sp_scrl_min.value(),
            "between_scrolls_max":    sp.sp_scrl_max.value(),
            "rest_min_minutes":       sp.sp_rest_min.value(),
            "rest_max_minutes":       sp.sp_rest_max.value(),
            "session_break_every":    sp.sp_switch_every.value(),
            "switch_mode":            "hours" if sp.rb_switch_hours.isChecked() else "profiles",
            "switch_hours":           sp.sp_switch_hours.value(),
        }
        return cfg

    def _load_cfg_into_ui(self):
        c  = self.cfg
        dp = self.dashboard_page
        fp = self.filters_page
        sp = self.settings_page

        targets_per_phone = c.get("targets_per_phone", [])
        if not targets_per_phone and "target_list" in c:
            old = c.get("target_list", [])
            targets_per_phone = [[] for _ in range(3)]
            for i, t in enumerate(old):
                targets_per_phone[i % 3].append(t)
            while len(targets_per_phone) < 3:
                targets_per_phone.append([])
        for i, tlist in enumerate(targets_per_phone[:3]):
            dp.target_rows[i].setPlainText("\n".join(tlist))

        dp.combo_mode.setCurrentText(c.get("last_mode", "followers"))
        dp.spin_count.setValue(int(c.get("last_count", 100)))

        s = c.get("schedule", {})
        dp.chk_schedule.setChecked(s.get("enabled", False))
        dp.time_start.setTime(QTime(s.get("start_hour", 8),  s.get("start_minute", 0)))
        dp.time_end.setTime(QTime(s.get("end_hour",   20),   s.get("end_minute",   0)))

        f = c.get("filters", {})
        fp.chk_skip_no_bio.setChecked(f.get("skip_no_bio", False))
        fp.chk_skip_private.setChecked(f.get("skip_private", False))
        fp.chk_skip_no_pic.setChecked(f.get("skip_no_profile_pic", False))
        fp.chk_skip_no_contact.setChecked(f.get("skip_no_contact", True))
        fp.spin_min_posts.setValue(int(f.get("min_posts", 0)))
        fp.chk_enable_post_spin.setChecked(f.get("enable_post_spin", False))
        fp.spin_skip_months.setValue(int(f.get("skip_no_posts_last_n_months", 1)))
        fp.spin_skip_months.setEnabled(fp.chk_enable_post_spin.isChecked())
        fp.txt_skip_keywords.setPlainText(", ".join(f.get("keywords", [])))
        fp.txt_only_keywords.setPlainText(", ".join(f.get("only_keywords", [])))

        sp.inp_sheet_id.setText(c.get("sheet_id", ""))
        sp.inp_sheet_tab.setText(c.get("sheet_tab", "Sheet1"))
        sp.inp_creds.setText(c.get("credentials_path", "assets/credentials.json"))
        sp.inp_webhook.setText(c.get("webhook_url", ""))
        sp.inp_appium_host.setText(c.get("appium", {}).get("host", "127.0.0.1"))

        d = c.get("delays", {})
        sp.sp_prof_min.setValue(d.get("between_profiles_min", 2.0))
        sp.sp_prof_max.setValue(d.get("between_profiles_max", 5.0))
        sp.sp_scrl_min.setValue(d.get("between_scrolls_min",  1.0))
        sp.sp_scrl_max.setValue(d.get("between_scrolls_max",  3.0))
        sp.sp_rest_min.setValue(int(d.get("rest_min_minutes", 30)))
        sp.sp_rest_max.setValue(int(d.get("rest_max_minutes", 60)))
        sp.sp_switch_every.setValue(int(d.get("session_break_every", 50)))
        switch_mode = d.get("switch_mode", "profiles")
        sp.rb_switch_hours.setChecked(switch_mode == "hours")
        sp.rb_switch_profiles.setChecked(switch_mode != "hours")
        sp.sp_switch_hours.setValue(int(d.get("switch_hours", 1)))
        # Re-apply enabled state after loading
        sp.sp_switch_every.setEnabled(switch_mode != "hours")
        sp.sp_switch_hours.setEnabled(switch_mode == "hours")

    # ── Core scraping ─────────────────────────────────────────────────────
    def _start_scraping(self):
        cfg      = self._collect_cfg()
        assigned = self._get_assigned_devices()

        if not assigned:
            InfoBar.warning("No Devices", "Assign at least one phone in the Dashboard.", parent=self)
            return

        phone_targets = self._get_phone_targets()
        total_targets = sum(len(t) for t in phone_targets)
        if total_targets == 0:
            InfoBar.warning(
                "No Targets",
                "Enter at least one username in any phone's target box.",
                parent=self,
            )
            return

        save_config(cfg)

        self._log("🔗 Connecting to Google Sheets…")
        # Disable Start button while OAuth may open a browser — re-enabled on failure
        self.dashboard_page.btn_start.setEnabled(False)

        _reuse = (
            self._sheets_client is not None
            and getattr(self._sheets_client, "_worksheet", None) is not None
            and getattr(self._sheets_client, "sheet_id", None) == cfg["sheet_id"]
        )

        # Capture everything _launch_scraping needs (closure over local vars)
        _cfg           = cfg
        _assigned      = assigned
        _phone_targets = phone_targets
        _total_targets = total_targets

        def _on_sheets_success(client):
            self._sheets_client = client
            self._log("✅ Google Sheets connected.")
            try:
                rows = client.get_row_count()
                self.settings_page.lbl_sheet_status.setText(f"✅ Connected · {rows} rows")
            except Exception:
                self.settings_page.lbl_sheet_status.setText("✅ Connected")
            self._launch_scraping(_cfg, _assigned, _phone_targets, _total_targets)

        def _on_sheets_failure(err_msg):
            self._sheets_client = None
            self.settings_page.lbl_sheet_status.setText("❌ Not connected")
            self._log(f"❌ Sheets auth failed: {err_msg}")
            InfoBar.error(
                "Google Sheets – Authentication Failed",
                err_msg,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                duration=6000,
                parent=self,
            )
            # Re-enable Start so the user can try again
            self._update_start_button_state()

        self._sheets_auth_worker = SheetsAuthWorker(
            credentials_path=_cfg["credentials_path"],
            sheet_id=_cfg["sheet_id"],
            tab_name=_cfg["sheet_tab"],
            existing_client=self._sheets_client,
            reuse=_reuse,
        )
        self._sheets_auth_worker.success.connect(_on_sheets_success)
        self._sheets_auth_worker.failure.connect(_on_sheets_failure)
        self._sheets_auth_worker.start()
        # _launch_scraping is called from _on_sheets_success when auth completes

    def _launch_scraping(self, cfg: dict, assigned: list, phone_targets: list, total_targets: int):
        """Called after Sheets auth succeeds. Starts Appium and PhoneWorkers."""
        serials = [s for _, s in assigned]
        self._log(f"🚀 Auto-starting Appium for {len(serials)} phone(s)…")
        try:
            serial_to_port = self._appium_mgr.start_for_devices(
                serials, log_callback=self._log
            )
        except RuntimeError as e:
            InfoBar.error("Appium Failed", str(e)[:300], parent=self)
            self._log(f"❌ Appium startup failed: {e}")
            self._update_start_button_state()
            return

        self._collected     = 0
        self._active_phones = len(assigned)
        self._done_phones   = 0
        self._workers       = []

        rp = self.results_page
        rp.table.setRowCount(0)
        rp.progress_bar.setMaximum(max(cfg["last_count"] * total_targets, 1))
        rp.progress_bar.setValue(0)
        rp.lbl_progress.setText("Starting…")
        rp.log_area.clear()

        self.dashboard_page.btn_start.setEnabled(False)
        self.dashboard_page.btn_stop.setEnabled(True)
        self._set_schedule_locked(True)
        self.dashboard_page.lbl_overall_status.setText(f"Running ({len(assigned)} phones)…")
        self.stackedWidget.setCurrentWidget(self.results_page)

        self._log(
            f"🚀 Starting {len(assigned)} phone(s) — "
            f"{total_targets} total targets, mode={cfg['last_mode']}, "
            f"max={cfg['last_count']} per target"
        )

        for row_idx, serial in assigned:
            port              = serial_to_port[serial]
            targets_for_phone = phone_targets[row_idx]

            if not targets_for_phone:
                self._log(f"ℹ️ Phone {row_idx+1} [{serial}] — no targets, skipping.")
                self._active_phones -= 1
                continue

            worker = PhoneWorker(
                phone_index=row_idx,
                serial=serial,
                appium_port=port,
                targets=targets_for_phone,
                config=cfg,
                sheets_client=self._sheets_client,
            )
            worker.signals.log.connect(self._log)
            worker.signals.account.connect(self._on_account)
            worker.signals.progress.connect(self._on_progress)
            worker.signals.finished.connect(self._on_phone_finished)
            worker.signals.error.connect(self._on_phone_error)
            worker.signals.status.connect(self._on_phone_status)
            worker.signals.account_switched.connect(self._on_auto_switch)
            self._workers.append(worker)
            worker.start()
            self._log(
                f"📱 Phone {row_idx+1} [{serial}] started on port {port} "
                f"with {len(targets_for_phone)} target(s)"
            )

        if not self._workers:
            self._reset_ui_after_done()

    def _stop_all(self):
        for w in self._workers:
            w.stop()
        self._log("⏹️ Stop requested for all phones…")
        self.dashboard_page.btn_stop.setEnabled(False)

    # ── Worker callbacks ──────────────────────────────────────────────────
    def _on_account(self, acc: dict):
        self._collected += 1
        rp    = self.results_page
        table = rp.table
        row   = table.rowCount()
        table.insertRow(row)
        cols = [
            acc.get("username",    ""),
            acc.get("full_name",   ""),
            acc.get("email",       ""),
            acc.get("phone",       ""),
            acc.get("country_code",""),
            acc.get("location",    ""),
            str(acc.get("followers",  "")),
            str(acc.get("following",  "")),
            str(acc.get("post_count", "")),
            acc.get("bio", "")[:80],
            acc.get("profile_url", ""),
            acc.get("scraped_at",  ""),
        ]
        for col, val in enumerate(cols):
            table.setItem(row, col, QTableWidgetItem(str(val)))
        table.scrollToBottom()
        rp.lbl_progress.setText(f"Collected: {self._collected}")
        rp.progress_bar.setValue(self._collected)
        if self._collected % 10 == 0:
            self._reload_blacklist_ui()

    def _on_progress(self, done: int, total: int):
        pass

    def _on_phone_status(self, phone_idx: int, status: str):
        dp = self.dashboard_page
        rp = self.results_page
        if phone_idx < len(dp.device_rows):
            dp.device_rows[phone_idx][3].setText(f"● {status}")
        if phone_idx < len(rp.phone_status_labels):
            rp.phone_status_labels[phone_idx].setText(f"Phone {phone_idx+1}: {status}")

    def _on_phone_finished(self, phone_idx: int, count: int):
        self._done_phones += 1
        self._log(
            f"✅ Phone {phone_idx+1} finished — {count} accounts. "
            f"({self._done_phones}/{self._active_phones} phones done)"
        )
        self._on_phone_status(phone_idx, "done ✅")
        if self._done_phones >= self._active_phones:
            self._all_done()

    def _on_phone_error(self, phone_idx: int, msg: str):
        self._done_phones += 1
        self._log(f"❌ Phone {phone_idx+1} error: {msg}")
        self._on_phone_status(phone_idx, "error ❌")
        InfoBar.error(f"Phone {phone_idx+1} Error", msg[:200], parent=self)
        if self._done_phones >= self._active_phones:
            self._all_done()

    def _all_done(self):
        self._log(f"🏁 All phones done. Total collected: {self._collected} accounts.")
        self._reload_blacklist_ui()
        InfoBar.success(
            "Complete!",
            f"All phones finished. {self._collected} accounts saved to Google Sheets.",
            parent=self,
        )
        self._reset_ui_after_done()

    def _reset_ui_after_done(self):
        self.dashboard_page.btn_start.setEnabled(True)
        self.dashboard_page.btn_stop.setEnabled(False)
        self._set_schedule_locked(False)
        self.dashboard_page.lbl_overall_status.setText(f"Done — {self._collected} collected")
        self.results_page.lbl_progress.setText(f"Done: {self._collected} total accounts")

    # ── Logging ───────────────────────────────────────────────────────────
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.results_page.log_area.append(f"[{ts}] {msg}")

    # ── Settings helpers ──────────────────────────────────────────────────
    def _browse_credentials(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select credentials.json", "", "JSON Files (*.json)"
        )
        if path:
            self.settings_page.inp_creds.setText(path)

    def _test_sheets(self):
        cfg = self._collect_cfg()
        save_config(cfg)
        if not cfg["sheet_id"]:
            InfoBar.warning("Missing Sheet ID", "Enter your Google Sheet ID first.", parent=self)
            return
        self._log("🔗 Authenticating with Google Sheets (browser may open)…")
        # Disable the test button while auth runs so the user can't double-click
        try:
            self.settings_page.btn_test_sheets.setEnabled(False)
        except Exception:
            pass

        def _on_test_success(client, rows):
            self._sheets_client = client
            self.settings_page.lbl_sheet_status.setText(f"✅ Connected · {rows} rows")
            self._log(f"✅ Google Sheet connected — {rows} existing rows.")
            InfoBar.success("Connected!", f"Google Sheets ready. {rows} existing rows.", parent=self)
            try:
                self.settings_page.btn_test_sheets.setEnabled(True)
            except Exception:
                pass

        def _on_test_failure(err_msg):
            self._sheets_client = None
            self.settings_page.lbl_sheet_status.setText("❌ Not connected")
            self._log(f"❌ Sheets error: {err_msg}")
            InfoBar.error("Connection Failed", err_msg, parent=self)
            try:
                self.settings_page.btn_test_sheets.setEnabled(True)
            except Exception:
                pass

        self._sheets_test_worker = SheetsTestWorker(
            credentials_path=cfg["credentials_path"],
            sheet_id=cfg["sheet_id"],
            tab_name=cfg["sheet_tab"],
        )
        self._sheets_test_worker.success.connect(_on_test_success)
        self._sheets_test_worker.failure.connect(_on_test_failure)
        self._sheets_test_worker.start()

    def _revoke_token(self):
        from src.sheets.google_sheets import TOKEN_PATH
        tp = os.path.abspath(TOKEN_PATH)
        if os.path.exists(tp):
            os.remove(tp)
            self._log("🔑 OAuth token revoked.")
            InfoBar.success("Token Revoked", "Will re-authenticate on next connect.", parent=self)
        else:
            InfoBar.info("No Token", "No saved token found.", parent=self)

    # ── Blacklist helpers ─────────────────────────────────────────────────
    def _reload_blacklist_ui(self):
        """Refresh the count label from the on-disk blacklist."""
        bl = load_blacklist()
        count = len(bl)
        self.filters_page.lbl_bl_count.setText(
            f"{count} entr{'y' if count == 1 else 'ies'} in blacklist"
        )

    def _download_blacklist_txt(self):
        """Export the current blacklist as a plain .txt file (one username per line)."""
        bl = load_blacklist()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Blacklist", "blacklist.txt", "Text files (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(bl)))
                if bl:
                    f.write("\n")
            InfoBar.success(
                "Downloaded",
                f"Blacklist exported ({len(bl)} entries) → {os.path.basename(path)}",
                parent=self,
            )
        except Exception as exc:
            InfoBar.error("Export Failed", str(exc), parent=self)

    def _import_blacklist_txt(self):
        """Import usernames from a .txt file (one per line) — merges with existing list."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Blacklist", "", "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                new_entries = {line.strip().lower() for line in f if line.strip()}
            bl = load_blacklist()
            before = len(bl)
            bl.update(new_entries)
            save_blacklist(bl)
            added = len(bl) - before
            self._reload_blacklist_ui()
            InfoBar.success(
                "Imported",
                f"Added {added} new entr{'y' if added == 1 else 'ies'} "
                f"({len(bl)} total in blacklist).",
                parent=self,
            )
        except Exception as exc:
            InfoBar.error("Import Failed", str(exc), parent=self)

    def _clear_blacklist(self):
        if QMessageBox.question(
            self, "Clear Blacklist",
            "Clear the entire blacklist?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            clear_blacklist()
            self._reload_blacklist_ui()
            InfoBar.success("Cleared", "Blacklist cleared.", parent=self)

    # ── Export ────────────────────────────────────────────────────────────
    def _export_csv(self):
        table = self.results_page.table
        if table.rowCount() == 0:
            InfoBar.warning("No Data", "No accounts collected yet.", parent=self)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", "scraped_accounts.csv", "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            headers = [
                table.horizontalHeaderItem(i).text()
                for i in range(table.columnCount())
            ]
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(headers)
                for row in range(table.rowCount()):
                    w.writerow([
                        (table.item(row, col).text() if table.item(row, col) else "")
                        for col in range(table.columnCount())
                    ])
            InfoBar.success("Exported", f"{table.rowCount()} rows saved to {path}", parent=self)
        except Exception as e:
            InfoBar.error("Export Failed", str(e), parent=self)

    # ── Cleanup ───────────────────────────────────────────────────────────
    def closeEvent(self, event):
        cfg = self._collect_cfg()
        cfg["mirror_width"] = getattr(self, "_mirror_width", 500)
        save_config(cfg)
        for w in self._workers:
            w.stop()
        for w in self._workers:
            w.wait(3000)
        self._appium_mgr.stop_all()
        if hasattr(self, "mirror"):
            self.mirror.detach()
        event.accept()


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    # Use the correct ICO path relative to the project root (two levels up from src/ui/)
    _ico = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "cansa_icon.ico"))
    if os.path.exists(_ico):
        app.setWindowIcon(QIcon(_ico))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())