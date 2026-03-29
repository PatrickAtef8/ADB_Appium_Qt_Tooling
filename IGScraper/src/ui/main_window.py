from __future__ import annotations

import csv
import os
import subprocess
import threading
import time
import traceback
import random
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore    import Qt, QThread, QTime, pyqtSignal, QObject
from PyQt6.QtGui     import QFont
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QSizePolicy, QTableWidgetItem,
    QVBoxLayout, QWidget,
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
# Typography helpers
# ─────────────────────────────────────────────────────────────────────────────

class T:
    @staticmethod
    def title():
        f = QFont("Segoe UI"); f.setPointSize(15); f.setWeight(QFont.Weight.Bold); return f
    @staticmethod
    def heading():
        f = QFont("Segoe UI"); f.setPointSize(12); f.setWeight(QFont.Weight.DemiBold); return f
    @staticmethod
    def body():
        f = QFont("Segoe UI"); f.setPointSize(10); return f
    @staticmethod
    def caption():
        f = QFont("Segoe UI"); f.setPointSize(9); return f
    @staticmethod
    def button():
        f = QFont("Segoe UI"); f.setPointSize(10); f.setWeight(QFont.Weight.DemiBold); return f
    @staticmethod
    def mono():
        f = QFont("Consolas"); f.setPointSize(9); return f


# ─────────────────────────────────────────────────────────────────────────────
# AccountDetectionWorker — detects Instagram accounts in background thread
# ─────────────────────────────────────────────────────────────────────────────

class AccountDetectionWorker(QThread):
    """
    Runs get_instagram_accounts() in a background thread so the UI never
    freezes while waiting for ADB + uiautomator to respond.

    Signals:
      finished(row_idx, accounts)  — emitted when detection completes
      error(row_idx)               — emitted if detection fails entirely
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

    Signals:
      finished(row_idx, success)  — emitted when switch attempt completes
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


# PhoneWorker — restored working logic from v1, signals unchanged
# ─────────────────────────────────────────────────────────────────────────────

class PhoneWorkerSignals(QObject):
    log      = pyqtSignal(str)
    account  = pyqtSignal(dict)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(int, int)
    error    = pyqtSignal(int, str)
    status   = pyqtSignal(int, str)


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
            # get_instagram_accounts uses monkey + uiautomator independently.
            # It must run before Appium takes control of the device, otherwise
            # the two sessions conflict.
            self._log("🔍 Detecting Instagram accounts…")
            device_accounts = get_instagram_accounts(self.serial)
            self._log(f"✅ Accounts found: {device_accounts}")
            acc_idx = 0

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

            self._scraper = InstagramScraper(
                controller=self._controller,
                on_account_found=on_account,
                on_log=self._log,
                on_progress=lambda d, t: self.signals.progress.emit(d, t),
            )

            delays         = cfg["delays"]
            filters        = cfg["filters"]
            mode           = cfg.get("last_mode", "followers")
            max_per_target = int(cfg.get("last_count", 100))
            schedule       = cfg.get("schedule", {})
            switch_every   = int(delays.get("session_break_every", 100))

            for target in self.targets:
                if self._stop_flag:
                    break
                if schedule.get("enabled"):
                    self._wait_for_schedule(schedule)
                if self._stop_flag:
                    break

                self._log(f"🎯 Processing target: @{target}")
                self.signals.status.emit(idx, f"@{target}")

                run_n = min(
                    random.randint(
                        int(delays.get("run_min_profiles", 3)),
                        int(delays.get("run_max_profiles", 10)),
                    ),
                    max_per_target,
                )
                count = self._scraper.run(
                    target_username=target,
                    mode=mode,
                    max_count=run_n,
                    filters=filters,
                    delays=delays,
                    fetch_details=True,
                    blacklist=blacklist,
                )
                total_collected += count
                self._log(f"✅ @{target} done — {count} this run, {total_collected} total")

                # ── Auto account switch ───────────────────────────────────────
                # Fires after each target batch when total_collected crosses
                # a switch_every boundary. Stops Appium first, switches via
                # monkey+uiautomator (same logic as manual switch), then
                # restarts Appium on the new account.
                if (len(device_accounts) > 1
                        and total_collected > 0
                        and switch_every > 0
                        and total_collected % switch_every == 0):
                    acc_idx = (acc_idx + 1) % len(device_accounts)
                    target_account = device_accounts[acc_idx]
                    self._log(f"🔄 Auto-switching to [{target_account}]…")
                    self._controller.stop_session()
                    switch_instagram_account(self.serial, target_account)
                    self._controller.start_session(self.serial)
                    self._log(f"✅ Resumed Appium on [{target_account}]")
                    time.sleep(3)

                if target != self.targets[-1] and not self._stop_flag:
                    rest_s = random.randint(
                        int(delays.get("rest_min_minutes", 30)) * 60,
                        int(delays.get("rest_max_minutes", 60)) * 60,
                    )
                    self._log(f"😴 Resting {rest_s // 60}m {rest_s % 60}s…")
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
        self.vBoxLayout.setContentsMargins(30, 18, 30, 30)
        self.vBoxLayout.setSpacing(14)
        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.setObjectName(title.replace(" ", ""))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lbl = TitleLabel(title, self)
        lbl.setFont(T.title())
        self.vBoxLayout.addWidget(lbl)

    def add(self, w):          self.vBoxLayout.addWidget(w)
    def add_layout(self, lay): self.vBoxLayout.addLayout(lay)
    def stretch(self):         self.vBoxLayout.addStretch(1)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard page — v2 UI (65/35 split with mirror placeholder)
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

        # ── LEFT PANEL (65%) ──────────────────────────────────────────────
        left_scroll = ScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setStyleSheet("ScrollArea{border:none;background:transparent;}")

        left_inner = QWidget()
        left_lay   = QVBoxLayout(left_inner)
        left_lay.setContentsMargins(20, 18, 12, 18)
        left_lay.setSpacing(12)
        left_scroll.setWidget(left_inner)

        title_lbl = TitleLabel("Dashboard", left_inner)
        title_lbl.setFont(T.title())
        left_lay.addWidget(title_lbl)

        # ── Device rows ───────────────────────────────────────────────────
        dev_card = CardWidget(left_inner)
        dev_lay  = QVBoxLayout(dev_card)
        dev_lay.setSpacing(8)

        hdr_row = QHBoxLayout()
        h1 = StrongBodyLabel("📱 Connected Phones", dev_card)
        h1.setFont(T.heading())
        hdr_row.addWidget(h1)
        hdr_row.addStretch()
        self.btn_refresh = PushButton(FIF.SYNC, "Refresh", dev_card)
        self.btn_refresh.setFont(T.button())
        self.btn_refresh.setFixedHeight(32)
        hdr_row.addWidget(self.btn_refresh)
        dev_lay.addLayout(hdr_row)

        note = CaptionLabel(
            "Appium starts automatically — no manual command needed.", dev_card
        )
        note.setFont(T.caption())
        dev_lay.addWidget(note)

        for i in range(3):
            row = QHBoxLayout()
            row.setSpacing(6)
            lbl_num = CaptionLabel(f"Phone {i+1}", dev_card)
            lbl_num.setFont(T.body()); lbl_num.setFixedWidth(52)
            combo_dev = ComboBox(dev_card)
            combo_dev.setFont(T.body()); combo_dev.setMinimumHeight(32)
            combo_dev.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo_acc = ComboBox(dev_card)
            combo_acc.setFont(T.caption()); combo_acc.setMinimumHeight(32)
            combo_acc.setFixedWidth(110); combo_acc.setPlaceholderText("Accounts")
            lbl_port = CaptionLabel(f":{4723 + i}", dev_card)
            lbl_port.setFont(T.caption()); lbl_port.setFixedWidth(62)
            lbl_status = CaptionLabel("● idle", dev_card)
            lbl_status.setFont(T.caption()); lbl_status.setFixedWidth(90)
            btn_view = PushButton("👁 View", dev_card)
            btn_view.setFont(T.button()); btn_view.setFixedWidth(80); btn_view.setFixedHeight(32)
            row.addWidget(lbl_num); row.addWidget(combo_dev); row.addWidget(combo_acc)
            row.addWidget(lbl_port); row.addWidget(lbl_status); row.addWidget(btn_view)
            dev_lay.addLayout(row)
            self.device_rows.append((combo_dev, combo_acc, lbl_port, lbl_status, btn_view))
        left_lay.addWidget(dev_card)

        # ── Targets ───────────────────────────────────────────────────────
        tgt_card = CardWidget(left_inner)
        tgt_lay  = QVBoxLayout(tgt_card)
        tgt_lay.setSpacing(8)
        tgt_lay.addWidget(StrongBodyLabel("🎯 Targets per Phone", tgt_card))
        tgt_lay.addWidget(CaptionLabel(
            "One username per line. Only phones with targets will run.", tgt_card
        ))
        for i in range(3):
            row = QHBoxLayout()
            lbl = StrongBodyLabel(f"P{i+1}:", tgt_card)
            lbl.setFont(T.body()); lbl.setFixedWidth(28)
            txt = TextEdit(tgt_card)
            txt.setFont(T.body())
            txt.setPlaceholderText("username1\nusername2")
            txt.setMinimumHeight(72); txt.setMaximumHeight(110)
            row.addWidget(lbl); row.addWidget(txt)
            tgt_lay.addLayout(row)
            self.target_rows.append(txt)
        left_lay.addWidget(tgt_card)

        # ── Mode + Count ──────────────────────────────────────────────────
        mode_card = CardWidget(left_inner)
        mode_lay  = QHBoxLayout(mode_card)
        mode_lay.setSpacing(16)
        mode_lay.addWidget(CaptionLabel("Mode:", mode_card))
        self.combo_mode = ComboBox(mode_card)
        self.combo_mode.addItems(["followers", "following"])
        self.combo_mode.setFont(T.body()); self.combo_mode.setMinimumHeight(32)
        mode_lay.addWidget(self.combo_mode)
        mode_lay.addWidget(CaptionLabel("Max/target:", mode_card))
        self.spin_count = SpinBox(mode_card)
        self.spin_count.setRange(1, 50000); self.spin_count.setValue(100)
        self.spin_count.setFont(T.body()); self.spin_count.setMinimumHeight(32)
        mode_lay.addWidget(self.spin_count)
        mode_lay.addStretch()
        left_lay.addWidget(mode_card)

        # ── Schedule ──────────────────────────────────────────────────────
        sched_card = CardWidget(left_inner)
        sched_lay  = QVBoxLayout(sched_card)
        sched_lay.setSpacing(6)
        sched_lay.addWidget(StrongBodyLabel("⏰ Working Hours", sched_card))
        self.chk_schedule = CheckBox("Enable schedule", sched_card)
        self.chk_schedule.setFont(T.body())
        sched_lay.addWidget(self.chk_schedule)
        row_t = QHBoxLayout()
        row_t.addWidget(CaptionLabel("From:", sched_card))
        self.time_start = TimeEdit(sched_card)
        self.time_start.setDisplayFormat("HH:mm"); self.time_start.setMinimumHeight(30)
        row_t.addWidget(self.time_start)
        row_t.addWidget(CaptionLabel("To:", sched_card))
        self.time_end = TimeEdit(sched_card)
        self.time_end.setDisplayFormat("HH:mm"); self.time_end.setMinimumHeight(30)
        row_t.addWidget(self.time_end)
        row_t.addStretch()
        sched_lay.addLayout(row_t)
        left_lay.addWidget(sched_card)

        # ── Controls ──────────────────────────────────────────────────────
        ctrl_row = QHBoxLayout()
        self.btn_start = PrimaryPushButton(FIF.PLAY, "START", left_inner)
        self.btn_start.setFont(T.button()); self.btn_start.setMinimumHeight(42)
        self.btn_stop  = PushButton(FIF.CLOSE, "STOP ALL", left_inner)
        self.btn_stop.setFont(T.button());  self.btn_stop.setMinimumHeight(42)
        self.btn_stop.setEnabled(False);    self.btn_stop.setObjectName("btn_stop_danger")
        self.lbl_overall_status = CaptionLabel("Ready", left_inner)
        self.lbl_overall_status.setFont(T.body())
        ctrl_row.addWidget(self.btn_start)
        ctrl_row.addWidget(self.btn_stop)
        ctrl_row.addSpacing(12)
        ctrl_row.addWidget(self.lbl_overall_status)
        ctrl_row.addStretch()
        left_lay.addLayout(ctrl_row)
        left_lay.addStretch(1)

        outer.addWidget(left_scroll, stretch=65)
        outer.addStretch(35)


# ─────────────────────────────────────────────────────────────────────────────
# Filters page — v2 UI (added only-keywords field)
# ─────────────────────────────────────────────────────────────────────────────

class FiltersPage(PageWidget):
    def __init__(self, parent=None):
        super().__init__("Filters & Blacklist", parent)
        self._build()

    def _build(self):
        # Skip conditions
        skip_card = CardWidget(self)
        skip_lay  = QVBoxLayout(skip_card)
        skip_lay.setSpacing(8)
        skip_lay.addWidget(StrongBodyLabel("🚫 Skip Conditions", skip_card))
        self.chk_skip_no_bio     = CheckBox("No bio", skip_card)
        self.chk_skip_private    = CheckBox("Private account", skip_card)
        self.chk_skip_no_pic     = CheckBox("No profile picture", skip_card)
        self.chk_skip_no_contact = CheckBox(
            "No email AND no phone in bio/contact (recommended)", skip_card
        )
        self.chk_skip_no_contact.setChecked(True)
        for chk in [self.chk_skip_no_bio, self.chk_skip_private,
                    self.chk_skip_no_pic, self.chk_skip_no_contact]:
            chk.setFont(T.body()); skip_lay.addWidget(chk)

        row_p = QHBoxLayout()
        from PyQt6.QtWidgets import QSpinBox as _FilterSpinBox
        self.spin_min_posts = _FilterSpinBox(skip_card)
        self.spin_min_posts.setRange(0, 10000)
        self.spin_min_posts.setFixedWidth(130)
        self.spin_min_posts.setFixedHeight(32)
        self.spin_min_posts.setFont(T.body())
        self.spin_min_posts.setKeyboardTracking(True)
        self.spin_min_posts.setStyleSheet("""
            QSpinBox {
                border: 1px solid #475569; border-radius: 6px;
                padding: 2px 6px; background: transparent;
            }
            QSpinBox:focus { border: 2px solid #3b82f6; }
        """)
        self.spin_recent_days = _FilterSpinBox(skip_card)
        self.spin_recent_days.setRange(0, 3650)
        self.spin_recent_days.setValue(365)
        self.spin_recent_days.setFixedWidth(130)
        self.spin_recent_days.setFixedHeight(32)
        self.spin_recent_days.setFont(T.body())
        self.spin_recent_days.setKeyboardTracking(True)
        self.spin_recent_days.setStyleSheet("""
            QSpinBox {
                border: 1px solid #475569; border-radius: 6px;
                padding: 2px 6px; background: transparent;
            }
            QSpinBox:focus { border: 2px solid #3b82f6; }
        """)
        row_p.addWidget(CaptionLabel("Min posts:", skip_card))
        row_p.addWidget(self.spin_min_posts)
        row_p.addSpacing(16)
        row_p.addWidget(CaptionLabel("Post within (days):", skip_card))
        row_p.addWidget(self.spin_recent_days)
        row_p.addStretch()
        skip_lay.addLayout(row_p)
        self.add(skip_card)

        # Keyword filtering
        kw_card = CardWidget(self)
        kw_lay  = QVBoxLayout(kw_card)
        kw_lay.setSpacing(8)
        kw_lay.addWidget(StrongBodyLabel("🔍 Keyword Filtering", kw_card))
        kw_lay.addWidget(CaptionLabel(
            "Skip profiles if Bio/Username contains these (comma-separated):", kw_card
        ))
        self.txt_skip_keywords = TextEdit(kw_card)
        self.txt_skip_keywords.setFont(T.body())
        self.txt_skip_keywords.setPlaceholderText("crypto, scam, bot, test")
        self.txt_skip_keywords.setMaximumHeight(80)
        kw_lay.addWidget(self.txt_skip_keywords)
        kw_lay.addWidget(CaptionLabel(
            "ONLY include profiles containing these keywords (blank = allow all):", kw_card
        ))
        self.txt_only_keywords = TextEdit(kw_card)
        self.txt_only_keywords.setFont(T.body())
        self.txt_only_keywords.setPlaceholderText("fitness, coach, realestate")
        self.txt_only_keywords.setMaximumHeight(80)
        kw_lay.addWidget(self.txt_only_keywords)
        self.add(kw_card)

        # Blacklist
        bl_card = CardWidget(self)
        bl_lay  = QVBoxLayout(bl_card)
        bl_lay.setSpacing(8)
        bl_lay.addWidget(StrongBodyLabel("🏴 Blacklist", bl_card))
        bl_lay.addWidget(CaptionLabel(
            "Usernames in this list will NEVER be scraped again. One per line.", bl_card
        ))
        self.txt_blacklist = TextEdit(bl_card)
        self.txt_blacklist.setFont(T.mono())
        self.txt_blacklist.setPlaceholderText("already_scraped_user1\nalready_scraped_user2")
        self.txt_blacklist.setMinimumHeight(150)
        bl_lay.addWidget(self.txt_blacklist)
        bl_btns = QHBoxLayout()
        self.btn_save_bl  = PrimaryPushButton(FIF.SAVE,   "Save Blacklist", bl_card)
        self.btn_load_bl  = PushButton(FIF.FOLDER,        "Reload",         bl_card)
        self.btn_clear_bl = PushButton(FIF.DELETE,        "Clear All",      bl_card)
        for b in [self.btn_save_bl, self.btn_load_bl, self.btn_clear_bl]:
            b.setFont(T.button()); bl_btns.addWidget(b)
        bl_btns.addStretch()
        bl_lay.addLayout(bl_btns)
        self.add(bl_card)
        self.stretch()


# ─────────────────────────────────────────────────────────────────────────────
# Results page — v2 UI (65/35 split with mirror placeholder)
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

        # ── LEFT PANEL (65%) ──────────────────────────────────────────────
        left_scroll = ScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setStyleSheet("ScrollArea{border:none;background:transparent;}")

        left_inner = QWidget()
        left_lay   = QVBoxLayout(left_inner)
        left_lay.setContentsMargins(20, 18, 12, 18)
        left_lay.setSpacing(12)
        left_scroll.setWidget(left_inner)

        title_lbl = TitleLabel("Results & Logs", left_inner)
        title_lbl.setFont(T.title())
        left_lay.addWidget(title_lbl)

        # Phone status
        status_card = CardWidget(left_inner)
        s_lay = QHBoxLayout(status_card)
        s_lay.setSpacing(16)
        s_lay.addWidget(StrongBodyLabel("📊 Phone Status:", status_card))
        for i in range(3):
            lbl = CaptionLabel(f"Phone {i+1}: idle", status_card)
            lbl.setFont(T.body())
            self.phone_status_labels.append(lbl)
            s_lay.addWidget(lbl)
        s_lay.addStretch()
        left_lay.addWidget(status_card)

        # Progress
        prog_card = CardWidget(left_inner)
        p_lay = QVBoxLayout(prog_card)
        p_lay.setSpacing(6)
        p_lay.addWidget(StrongBodyLabel("📈 Overall Progress", prog_card))
        self.progress_bar = ProgressBar(prog_card)
        self.progress_bar.setValue(0); self.progress_bar.setMinimumHeight(12)
        p_lay.addWidget(self.progress_bar)
        self.lbl_progress = CaptionLabel("Ready", prog_card)
        self.lbl_progress.setFont(T.body())
        p_lay.addWidget(self.lbl_progress)
        left_lay.addWidget(prog_card)

        # Results table
        left_lay.addWidget(StrongBodyLabel("📋 Collected Accounts", left_inner))
        self.table = TableWidget(left_inner)
        self.table.setColumnCount(12)
        self.table.setHorizontalHeaderLabels([
            "Username", "Full Name", "Email", "Phone", "Country",
            "Location", "Followers", "Following", "Posts", "Bio",
            "Profile URL", "Scraped At",
        ])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setMinimumHeight(240)
        self.table.setFont(T.body())
        left_lay.addWidget(self.table)

        exp_row = QHBoxLayout()
        self.btn_export_csv = PushButton(FIF.DOWNLOAD, "Export CSV", left_inner)
        self.btn_export_csv.setFont(T.button())
        exp_row.addWidget(self.btn_export_csv); exp_row.addStretch()
        left_lay.addLayout(exp_row)

        # Activity log
        left_lay.addWidget(StrongBodyLabel("📜 Activity Log", left_inner))
        self.log_area = TextEdit(left_inner)
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(200)
        self.log_area.setFont(T.mono())
        left_lay.addWidget(self.log_area)
        left_lay.addStretch(1)

        outer.addWidget(left_scroll, stretch=65)
        outer.addStretch(35)


# ─────────────────────────────────────────────────────────────────────────────
# Settings page — v2 UI (sp_switch_every instead of sp_break_dur)
# ─────────────────────────────────────────────────────────────────────────────

class SettingsPage(PageWidget):
    def __init__(self, parent=None):
        super().__init__("Settings", parent)
        self._build()

    def _build(self):
        # Google Sheets
        sh_card = CardWidget(self)
        sh_lay  = QVBoxLayout(sh_card)
        sh_lay.setSpacing(8)
        sh_lay.addWidget(StrongBodyLabel("📊 Google Sheets", sh_card))

        def _row(label, widget):
            r = QHBoxLayout()
            lbl = CaptionLabel(label, sh_card); lbl.setFixedWidth(100)
            r.addWidget(lbl); r.addWidget(widget)
            sh_lay.addLayout(r)

        self.inp_sheet_id  = LineEdit(sh_card); self.inp_sheet_id.setMinimumHeight(32)
        self.inp_sheet_id.setPlaceholderText("Spreadsheet ID from URL")
        self.inp_sheet_tab = LineEdit(sh_card); self.inp_sheet_tab.setMinimumHeight(32)
        self.inp_creds     = LineEdit(sh_card); self.inp_creds.setMinimumHeight(32)
        _row("Sheet ID:",  self.inp_sheet_id)
        _row("Tab Name:",  self.inp_sheet_tab)

        creds_row = QHBoxLayout()
        creds_lbl = CaptionLabel("Credentials:", sh_card); creds_lbl.setFixedWidth(100)
        self.btn_browse_creds = PushButton(FIF.FOLDER, "Browse", sh_card)
        self.btn_browse_creds.setFixedHeight(32)
        creds_row.addWidget(creds_lbl)
        creds_row.addWidget(self.inp_creds)
        creds_row.addWidget(self.btn_browse_creds)
        sh_lay.addLayout(creds_row)

        btns_row = QHBoxLayout()
        self.btn_test_sheets  = PrimaryPushButton(FIF.SEND,   "Connect & Auth", sh_card)
        self.btn_revoke_token = PushButton(FIF.DELETE,        "Revoke Token",   sh_card)
        self.lbl_sheet_status = CaptionLabel("Not connected", sh_card)
        for b in [self.btn_test_sheets, self.btn_revoke_token]:
            b.setFixedHeight(32); btns_row.addWidget(b)
        btns_row.addWidget(self.lbl_sheet_status); btns_row.addStretch()
        sh_lay.addLayout(btns_row)
        self.add(sh_card)

        # Webhook
        wh_card = CardWidget(self)
        wh_lay  = QVBoxLayout(wh_card)
        wh_lay.addWidget(StrongBodyLabel("🔗 Webhook", wh_card))
        wh_lay.addWidget(CaptionLabel("POST each account as JSON (blank = disabled).", wh_card))
        wh_row = QHBoxLayout()
        wh_row.addWidget(CaptionLabel("URL:", wh_card))
        self.inp_webhook = LineEdit(wh_card)
        self.inp_webhook.setPlaceholderText("https://hooks.zapier.com/…")
        self.inp_webhook.setMinimumHeight(32)
        wh_row.addWidget(self.inp_webhook)
        wh_lay.addLayout(wh_row)
        self.add(wh_card)

        # Appium
        ap_card = CardWidget(self)
        ap_lay  = QVBoxLayout(ap_card)
        ap_lay.addWidget(StrongBodyLabel("⚙️ Appium", ap_card))
        ap_lay.addWidget(CaptionLabel(
            "Ports: Phone 1=4723, Phone 2=4724, Phone 3=4725. "
            "Change host only for remote Appium.", ap_card
        ))
        ap_row = QHBoxLayout()
        ap_row.addWidget(CaptionLabel("Host:", ap_card))
        self.inp_appium_host = LineEdit(ap_card)
        self.inp_appium_host.setMinimumHeight(32)
        ap_row.addWidget(self.inp_appium_host)
        ap_lay.addLayout(ap_row)
        self.add(ap_card)

        # Delays
        dl_card = CardWidget(self)
        dl_lay  = QVBoxLayout(dl_card)
        dl_lay.addWidget(StrongBodyLabel("⏱️ Randomized Delays", dl_card))
        dl_lay.addWidget(CaptionLabel("All delays randomized between MIN and MAX.", dl_card))

        def add_delay(label, attr_min, attr_max, mn, mx, is_int=False):
            from PyQt6.QtWidgets import QSpinBox, QDoubleSpinBox
            r = QHBoxLayout()
            r.addWidget(CaptionLabel(label, dl_card))
            if is_int:
                wmin = QSpinBox(dl_card)
                wmin.setRange(int(mn), int(mx))
                wmax = QSpinBox(dl_card)
                wmax.setRange(int(mn), int(mx))
            else:
                wmin = QDoubleSpinBox(dl_card)
                wmin.setRange(mn, mx)
                wmin.setDecimals(1)
                wmin.setSingleStep(0.5)
                wmax = QDoubleSpinBox(dl_card)
                wmax.setRange(mn, mx)
                wmax.setDecimals(1)
                wmax.setSingleStep(0.5)
            for w in (wmin, wmax):
                w.setFont(T.body())
                w.setFixedHeight(32)
                w.setFixedWidth(110)
                w.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
                w.setKeyboardTracking(True)
                w.setStyleSheet("""
                    QSpinBox, QDoubleSpinBox {
                        border: 1px solid #475569;
                        border-radius: 6px;
                        padding: 2px 6px;
                        background: transparent;
                    }
                    QSpinBox:focus, QDoubleSpinBox:focus {
                        border: 2px solid #3b82f6;
                    }
                """)
            r.addWidget(CaptionLabel("MIN", dl_card)); r.addWidget(wmin)
            r.addSpacing(8)
            r.addWidget(CaptionLabel("MAX", dl_card)); r.addWidget(wmax)
            r.addStretch()
            dl_lay.addLayout(r)
            setattr(self, attr_min, wmin); setattr(self, attr_max, wmax)

        add_delay("Between profiles (s):", "sp_prof_min", "sp_prof_max", 1.0, 30.0)
        add_delay("Between scrolls (s):",  "sp_scrl_min", "sp_scrl_max", 0.5, 15.0)
        add_delay("Profiles per run:",      "sp_run_min",  "sp_run_max",  1, 500, True)
        add_delay("Rest between runs (m):", "sp_rest_min", "sp_rest_max", 1, 1440, True)

        brk_row = QHBoxLayout()
        brk_row.addWidget(CaptionLabel("Switch account every (profiles):", dl_card))
        from PyQt6.QtWidgets import QSpinBox as _QSpinBox
        self.sp_switch_every = _QSpinBox(dl_card)
        self.sp_switch_every.setRange(1, 1000)
        self.sp_switch_every.setFont(T.body())
        self.sp_switch_every.setFixedHeight(32)
        self.sp_switch_every.setFixedWidth(110)
        self.sp_switch_every.setKeyboardTracking(True)
        self.sp_switch_every.setStyleSheet("""
            QSpinBox {
                border: 1px solid #475569;
                border-radius: 6px;
                padding: 2px 6px;
                background: transparent;
            }
            QSpinBox:focus { border: 2px solid #3b82f6; }
        """)
        brk_row.addWidget(self.sp_switch_every); brk_row.addStretch()
        dl_lay.addLayout(brk_row)
        self.add(dl_card)
        self.stretch()


# ─────────────────────────────────────────────────────────────────────────────
# MainWindow — v2 UI + v1 scraping logic fully restored
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.cfg                = load_config()
        self._workers:          List[PhoneWorker]      = []
        self._collected         = 0
        self._active_phones     = 0
        self._done_phones       = 0
        self._mirror_phone_idx: Optional[int]          = None
        self._scrcpy_procs:     Dict[str, subprocess.Popen] = {}
        self._appium_mgr        = AppiumManager()
        self._sheets_client:    Optional[SheetsClient] = None
        # Keeps references to running AccountDetectionWorker threads so they
        # are not garbage-collected before they finish.
        self._detection_workers: Dict[int, AccountDetectionWorker] = {}
        # Keeps references to running AccountSwitchWorker threads.
        self._switch_workers: Dict[int, AccountSwitchWorker] = {}

        self.setWindowTitle("Instagram Scraper Pro")
        self.resize(1360, 900)

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

    # ── Persistent mirror panel ───────────────────────────────────────────
    def _init_persistent_mirror(self):
        self.mirror_panel = QWidget(self)
        self.mirror_panel.setObjectName("PersistentMirrorPanel")
        self.mirror_panel.setFixedWidth(450)

        layout = QVBoxLayout(self.mirror_panel)
        layout.setContentsMargins(8, 18, 18, 18)
        layout.setSpacing(10)

        hdr_row = QHBoxLayout()
        mirror_title = StrongBodyLabel("📺 Live Mirror", self.mirror_panel)
        mirror_title.setFont(T.heading())
        hdr_row.addWidget(mirror_title)
        hdr_row.addStretch()
        self.lbl_mirror_device = CaptionLabel("No device selected", self.mirror_panel)
        self.lbl_mirror_device.setFont(T.caption())
        hdr_row.addWidget(self.lbl_mirror_device)
        layout.addLayout(hdr_row)

        self.mirror = MirrorWidget(phone_index=0, parent=self.mirror_panel)
        self.mirror.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.mirror.detached.connect(self._on_mirror_detached)
        layout.addWidget(self.mirror, stretch=1)
        self.mirror_panel.hide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "mirror_panel"):
            tb_h = self.titleBar.height()
            self.mirror_panel.setGeometry(
                self.width() - self.mirror_panel.width(),
                tb_h,
                self.mirror_panel.width(),
                self.height() - tb_h,
            )

    def _update_mirror_visibility(self):
        current = self.stackedWidget.currentWidget()
        show = current in [self.dashboard_page, self.results_page]
        self.mirror_panel.setVisible(show)

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

    # ── Theme + Toggle Button (FIXED) ─────────────────────────────────────
    def _init_theme(self):
        setTheme(Theme.DARK)
        setThemeColor("#3b82f6")
        self._apply_stylesheet()
        
        # ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
        # THE LIGHT/DARK BUTTON IS HERE
        btn = TransparentToolButton(FIF.BRUSH, self)
        btn.clicked.connect(self._toggle_theme)
        self.titleBar.hBoxLayout.insertWidget(0, btn, 0, Qt.AlignmentFlag.AlignLeft)
        # ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←

        if hasattr(self, "mirror"):
            self.mirror.update_theme()

    def _apply_stylesheet(self):
        dark = isDarkTheme()
        if dark:
            css = """
                QWidget{background:#0f172a;color:#f8fafc}
                ScrollArea{background:#0f172a;border:none}
                QLabel,TitleLabel,StrongBodyLabel,CaptionLabel{color:#f8fafc;background:transparent}
                CardWidget{background:#1e293b;border:1px solid #334155;border-radius:10px}
                LineEdit,SpinBox,DoubleSpinBox,ComboBox,TextEdit,TimeEdit{
                    background:#0f172a;border:1px solid #334155;border-radius:7px;
                    color:#f8fafc;padding:4px;font-size:10pt}
                LineEdit:focus,SpinBox:focus,ComboBox:focus,TimeEdit:focus{border:2px solid #3b82f6}
                TextEdit:focus{border:2px solid #3b82f6}
                PushButton{background:#334155;border:1px solid #475569;border-radius:7px;
                    color:#f8fafc;padding:4px 12px;font-size:10pt}
                PushButton:hover{background:#475569}
                PushButton#btn_stop_danger{background:#dc2626;border:1px solid #991b1b;
                    color:#fff;font-weight:bold}
                PushButton#btn_stop_danger:hover{background:#b91c1c}
                PrimaryPushButton{background:#3b82f6;border:none;border-radius:7px;
                    color:#fff;font-weight:bold;padding:4px 12px;font-size:10pt}
                PrimaryPushButton:hover{background:#2563eb}
                TableWidget{background:#1e293b;border:1px solid #334155;border-radius:7px;
                    color:#cbd5e1;gridline-color:#334155;font-size:9pt}
                TableWidget::item{padding:4px;background:#1e293b;color:#cbd5e1}
                TableWidget::item:selected{background:#334155;color:#f8fafc}
                QHeaderView::section{background:#0f172a;color:#f8fafc;border:none;
                    border-right:1px solid #334155;padding:5px;font-weight:bold;font-size:9pt}
                ProgressBar{background:#334155;border:1px solid #475569;border-radius:3px}
                ProgressBar::chunk{background:#3b82f6;border-radius:2px}
                CheckBox{color:#f8fafc;background:transparent;font-size:10pt}
                QScrollBar:vertical{background:#0f172a;width:7px;border:none}
                QScrollBar::handle:vertical{background:#475569;border-radius:3px;min-height:16px}
                QComboBox QAbstractItemView{background:#1e293b;color:#f8fafc;
                    selection-background-color:#3b82f6;border:1px solid #334155}
                #PersistentMirrorPanel{background:#0f172a;border-left:1px solid #334155}
            """
        else:
            css = """
                QWidget{background:#f8fafc;color:#0f172a}
                CardWidget{background:#fff;border:1px solid #e2e8f0;border-radius:10px}
                LineEdit,SpinBox,DoubleSpinBox,ComboBox,TextEdit,TimeEdit{
                    background:#f1f5f9;border:1px solid #e2e8f0;border-radius:7px;
                    color:#0f172a;font-size:10pt}
                PushButton{background:#e2e8f0;border:1px solid #cbd5e1;border-radius:7px;
                    color:#0f172a;font-size:10pt}
                PushButton#btn_stop_danger{background:#dc2626;border:none;color:#fff;font-weight:bold}
                PrimaryPushButton{background:#3b82f6;color:#fff;border-radius:7px;
                    font-weight:bold;font-size:10pt}
                TableWidget{background:#fff;color:#0f172a;border:1px solid #e2e8f0;font-size:9pt}
                QHeaderView::section{background:#f1f5f9;color:#0f172a;border:none;font-size:9pt}
                CheckBox{color:#0f172a;font-size:10pt}
                #PersistentMirrorPanel{background:#f8fafc;border-left:1px solid #e2e8f0}
            """
        self.setStyleSheet(css)

    def _toggle_theme(self):
        setTheme(Theme.LIGHT if isDarkTheme() else Theme.DARK)
        self._apply_stylesheet()
        if hasattr(self, "mirror"):
            self.mirror.update_theme()

    # ── Signal connections ────────────────────────────────────────────────
    def _connect_signals(self):
        dp = self.dashboard_page
        fp = self.filters_page
        sp = self.settings_page
        rp = self.results_page

        dp.btn_refresh.clicked.connect(self._refresh_devices)
        dp.btn_start.clicked.connect(self._start_scraping)
        dp.btn_stop.clicked.connect(self._stop_all)

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
        fp.btn_save_bl.clicked.connect(self._save_blacklist_from_ui)
        fp.btn_load_bl.clicked.connect(self._reload_blacklist_ui)
        fp.btn_clear_bl.clicked.connect(self._clear_blacklist)
        rp.btn_export_csv.clicked.connect(self._export_csv)

    # ── Device helpers ────────────────────────────────────────────────────
    def _refresh_devices(self):
        devices = get_connected_devices()
        dp = self.dashboard_page
        for i, (combo_dev, combo_acc, lbl_port, lbl_status, btn) in enumerate(dp.device_rows):
            combo_dev.blockSignals(True)
            combo_dev.clear()
            combo_dev.addItem("(not assigned)", userData=None)
            for serial, model in devices:
                combo_dev.addItem(f"{model} [{serial}]", userData=serial)
            # Always start unassigned — never restore last session's device choice
            combo_dev.setCurrentIndex(0)
            combo_dev.blockSignals(False)
            self._on_device_selected(i)

        # Show a slide-in notification listing connected devices
        if devices:
            names = ", ".join(model for _, model in devices)
            count = len(devices)
            label = "device" if count == 1 else "devices"
            InfoBar.success(
                title=f"{count} {label} connected",
                content=f"{names} — select a slot to assign.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                duration=4000,
                parent=self,
            )
        else:
            InfoBar.warning(
                title="No devices found",
                content="Connect a phone via USB and press Refresh.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                duration=4000,
                parent=self,
            )
        self._log(f"🔍 Found {len(devices)} device(s).")

    def _on_device_selected(self, idx: int):
        dp = self.dashboard_page
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[idx]
        serial = combo_dev.currentData()

        # Clear previous accounts immediately
        combo_acc.clear()

        if not serial:
            return

        # Cancel any previous detection still running for this slot
        old_worker = self._detection_workers.pop(idx, None)
        if old_worker and old_worker.isRunning():
            old_worker.terminate()
            old_worker.wait(500)

        # Show loading state in the combo and status label
        combo_acc.setPlaceholderText("Detecting…")
        combo_acc.setEnabled(False)
        lbl_status.setText("⏳ detecting")

        # Run detection in background — UI stays fully responsive
        worker = AccountDetectionWorker(row_idx=idx, serial=serial)
        worker.finished.connect(self._on_accounts_detected)
        worker.error.connect(self._on_accounts_error)
        self._detection_workers[idx] = worker
        worker.start()

    def _on_accounts_detected(self, row_idx: int, accounts: list):
        """Called on the main thread when AccountDetectionWorker finishes."""
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

        # Clean up worker reference
        self._detection_workers.pop(row_idx, None)

    def _on_accounts_error(self, row_idx: int):
        """Called on the main thread when AccountDetectionWorker fails."""
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

    def _on_account_selected(self, row_idx: int):
        """
        Called when the user picks an account from the combo_acc dropdown.
        Runs switch_instagram_account in a background thread — UI stays responsive.
        Ignored during account detection (combo is disabled then) and when
        the combo is being populated programmatically.
        """
        dp = self.dashboard_page
        if row_idx >= len(dp.device_rows):
            return

        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]

        # Ignore if combo is disabled (detection in progress) or no device
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

        # Cancel any previous switch still running for this slot
        old = self._switch_workers.pop(row_idx, None)
        if old and old.isRunning():
            old.terminate()
            old.wait(500)

        # Show switching state
        lbl_status.setText("⏳ switching")
        combo_acc.setEnabled(False)

        worker = AccountSwitchWorker(
            row_idx=row_idx,
            serial=serial,
            account_name=account_name,
        )
        worker.finished.connect(self._on_switch_done)
        self._switch_workers[row_idx] = worker
        worker.start()

    def _on_switch_done(self, row_idx: int, success: bool):
        """Called on the main thread when AccountSwitchWorker finishes."""
        dp = self.dashboard_page
        if row_idx >= len(dp.device_rows):
            return

        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]
        combo_acc.setEnabled(True)

        if success:
            lbl_status.setText("● idle")
            account_name = combo_acc.currentText()
            self._log(f"✅ [Phone {row_idx + 1}] Switched to @{account_name}")
        else:
            lbl_status.setText("⚠ switch failed")
            self._log(f"⚠️ [Phone {row_idx + 1}] Account switch failed")

        self._switch_workers.pop(row_idx, None)

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
            InfoBar.warning(
                "No Device", f"Phone {row_idx + 1} has no device assigned.", parent=self
            )
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

    # ── Config collect / load — v1 keys preserved ─────────────────────────
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
            "require_recent_post_days": fp.spin_recent_days.value(),
            "keywords":                 parse_keywords(fp.txt_skip_keywords.toPlainText()),
            "only_keywords":            parse_keywords(fp.txt_only_keywords.toPlainText()),
        }

        cfg["sheet_id"]          = sp.inp_sheet_id.text().strip()
        cfg["sheet_tab"]         = sp.inp_sheet_tab.text().strip() or "Sheet1"
        cfg["credentials_path"]  = sp.inp_creds.text().strip()
        cfg["webhook_url"]       = sp.inp_webhook.text().strip()
        cfg["appium"]["host"]    = sp.inp_appium_host.text().strip()

        cfg["delays"] = {
            "between_profiles_min":  sp.sp_prof_min.value(),
            "between_profiles_max":  sp.sp_prof_max.value(),
            "between_scrolls_min":   sp.sp_scrl_min.value(),
            "between_scrolls_max":   sp.sp_scrl_max.value(),
            "run_min_profiles":      sp.sp_run_min.value(),
            "run_max_profiles":      sp.sp_run_max.value(),
            "rest_min_minutes":      sp.sp_rest_min.value(),
            "rest_max_minutes":      sp.sp_rest_max.value(),
            "session_break_every":   sp.sp_switch_every.value(),
            "session_break_duration": 30,
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
        fp.spin_recent_days.setValue(int(f.get("require_recent_post_days", 365)))
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
        sp.sp_run_min.setValue(int(d.get("run_min_profiles",  5)))
        sp.sp_run_max.setValue(int(d.get("run_max_profiles",  15)))
        sp.sp_rest_min.setValue(int(d.get("rest_min_minutes", 30)))
        sp.sp_rest_max.setValue(int(d.get("rest_max_minutes", 60)))
        sp.sp_switch_every.setValue(int(d.get("session_break_every", 50)))

    # ── Core scraping — fully restored from v1 ────────────────────────────
    def _start_scraping(self):
        cfg      = self._collect_cfg()
        assigned = self._get_assigned_devices()

        if not assigned:
            InfoBar.warning(
                "No Devices", "Assign at least one phone in the Dashboard.", parent=self
            )
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
        try:
            self._sheets_client = SheetsClient(
                credentials_path=cfg["credentials_path"],
                sheet_id=cfg["sheet_id"],
                tab_name=cfg["sheet_tab"],
            )
            self._sheets_client.authenticate()
            self._sheets_client.connect_sheet()
            self._log("✅ Google Sheets connected.")
        except Exception as e:
            InfoBar.error("Sheets Failed", str(e)[:200], parent=self)
            self._log(f"❌ Sheets error: {e}")
            return

        serials = [s for _, s in assigned]
        self._log(f"🚀 Auto-starting Appium for {len(serials)} phone(s)…")
        try:
            serial_to_port = self._appium_mgr.start_for_devices(
                serials, log_callback=self._log
            )
        except RuntimeError as e:
            InfoBar.error("Appium Failed", str(e)[:300], parent=self)
            self._log(f"❌ Appium startup failed: {e}")
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
        self.dashboard_page.lbl_overall_status.setText(
            f"Running ({len(assigned)} phones)…"
        )
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
        self.dashboard_page.lbl_overall_status.setText(
            f"Done — {self._collected} collected"
        )
        self.results_page.lbl_progress.setText(
            f"Done: {self._collected} total accounts"
        )

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
        try:
            client = SheetsClient(
                credentials_path=cfg["credentials_path"],
                sheet_id=cfg["sheet_id"],
                tab_name=cfg["sheet_tab"],
            )
            client.authenticate()
            client.connect_sheet()
            rows = client.get_row_count()
            self.settings_page.lbl_sheet_status.setText(f"✅ Connected · {rows} rows")
            self._log(f"✅ Google Sheet connected — {rows} existing rows.")
            InfoBar.success("Connected!", f"Google Sheets ready. {rows} existing rows.", parent=self)
        except Exception as e:
            self.settings_page.lbl_sheet_status.setText("❌ Failed")
            self._log(f"❌ Sheets error: {e}")
            InfoBar.error("Connection Failed", str(e)[:200], parent=self)

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
        bl = load_blacklist()
        self.filters_page.txt_blacklist.setPlainText("\n".join(sorted(bl)))

    def _save_blacklist_from_ui(self):
        raw = self.filters_page.txt_blacklist.toPlainText()
        bl  = {u.strip().lower() for u in raw.splitlines() if u.strip()}
        save_blacklist(bl)
        InfoBar.success("Saved", f"Blacklist saved ({len(bl)} entries).", parent=self)

    def _clear_blacklist(self):
        if QMessageBox.question(
            self, "Clear Blacklist",
            "Clear the entire blacklist?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            clear_blacklist()
            self.filters_page.txt_blacklist.clear()
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
            InfoBar.success(
                "Exported", f"{table.rowCount()} rows saved to {path}", parent=self
            )
        except Exception as e:
            InfoBar.error("Export Failed", str(e), parent=self)

    # ── Cleanup ───────────────────────────────────────────────────────────
    def closeEvent(self, event):
        save_config(self._collect_cfg())
        for w in self._workers:
            w.stop()
        for w in self._workers:
            w.wait(3000)
        self._appium_mgr.stop_all()
        if hasattr(self, "mirror"):
            self.mirror.detach()
        event.accept()