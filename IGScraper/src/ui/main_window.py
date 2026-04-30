import csv
import os
import subprocess
import threading
import time
import traceback
import random
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore    import Qt, QThread, QTime, QTimer, pyqtSignal, QObject, QUrl
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
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
from src.utils.blacklist            import (
    clear_blacklist, load_blacklist, save_blacklist,
    clear_keyword_blacklist, load_keyword_blacklist, save_keyword_blacklist,
)
from src.utils.completed            import (
    start_session, record_scraped, mark_target_completed,
    finish_session, get_summary_path, summary_exists,
)
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

# ── Phone slot limit ──────────────────────────────────────────────────────────
MAX_PHONES = 10   # maximum simultaneous phone / emulator slots


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
    target_done     = pyqtSignal(int, str)    # (phone_index, target_username) — target fully scraped
    scraped_count   = pyqtSignal(int, int)    # (phone_index, session_total) — live count per phone


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
                # Google Sheets is optional — sheets is None when user disabled it
                if self.sheets is not None:
                    try:
                        saved = self.sheets.append_account(acc)
                    except Exception as sheets_exc:
                        self._log(f"⚠️ Sheets write failed (skipping): {sheets_exc}")
                        saved = True   # still emit so webhook fires
                else:
                    saved = True       # no dedup via sheets; always treat as new
                if saved:
                    self.signals.account.emit(acc)
                    # Update per-phone session counter
                    nonlocal total_collected
                    self.signals.scraped_count.emit(self.phone_index, total_collected + 1)
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
                # pauses as soon as the end time is reached, not just between
                # targets.  We only stop the *scraper* here (so the current
                # target loop exits cleanly).  The outer "for target in targets"
                # loop already calls _wait_for_schedule() at the top of every
                # iteration, so it will automatically block until the next
                # window opens — no extra logic needed here.
                # We do NOT touch self._stop_flag: if the user pressed Stop All,
                # that flag is already True and must stay True so the outer loop
                # exits rather than resuming scraping.
                if schedule.get("enabled") and self._is_past_schedule_end(schedule):
                    end = dtime(schedule["end_hour"], schedule["end_minute"])
                    self._log(f"⏰ Working hours ended ({end:%H:%M}). Stopping.")
                    if self._scraper:
                        self._scraper.stop()
                    self._stop_flag = True
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

                # ── Human warm-up scroll (2-5 min on home feed) ──────────────
                # Makes the new session look organic before jumping into scraping.
                if self._scraper and not self._stop_flag:
                    self._scraper._human_warmup_scroll(120, 300)

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

            # ── Target loop ──────────────────────────────────────────────────
            # We use an index instead of `for target in self.targets` so that
            # when working hours end mid-scrape we can re-run the SAME target
            # (with the remaining quota) after the next window opens, rather
            # than silently advancing to the next target or stopping entirely.
            target_idx = 0
            while target_idx < len(self.targets) and not self._stop_flag:
                target = self.targets[target_idx]

                if schedule.get("enabled"):
                    self._wait_for_schedule(schedule)
                if self._stop_flag:
                    break

                self._log(f"🎯 Processing target: @{target}")
                self.signals.status.emit(idx, f"@{target}")

                # Clear both stop flags before (re-)running a target.
                # _hours_interrupted is set by _check_and_switch when the
                # schedule end fires; we read it after run() returns to decide
                # whether to re-queue this target.
                if self._scraper:
                    self._scraper._stop_flag = False
                    self._scraper._hours_interrupted = False

                # ── Retry loop for navigation failures ───────────────────
                # scraper.run() returns 0 without raising when it cannot open
                # the profile or the follower/following list (network hiccup,
                # Instagram rate-limit, stale Appium state, etc.). Retry up to
                # 2 extra times with a 30 s cool-down before giving up, so a
                # single transient failure doesn't silently skip the whole target.
                # We never retry if the user pressed Stop.
                _MAX_TARGET_RETRIES = 2
                count = 0
                for _attempt in range(1 + _MAX_TARGET_RETRIES):
                    if self._stop_flag:
                        break
                    if _attempt > 0:
                        self._log(
                            f"⚠️ @{target} returned 0 accounts — "
                            f"retrying in 30 s (attempt {_attempt}/{_MAX_TARGET_RETRIES})…"
                        )
                        self._sleep(30)
                        if self._stop_flag:
                            break
                        if self._scraper:
                            self._scraper._stop_flag = False
                            self._scraper._hours_interrupted = False

                    count = self._scraper.run(
                        target_username=target,
                        mode=mode,
                        max_count=max_per_target,
                        filters=filters,
                        delays=delays,
                        fetch_details=True,
                        blacklist=blacklist,
                    )
                    # Don't retry if: user stopped, hours interrupted (will
                    # re-run whole target next window), or real accounts found.
                    hours_hit = getattr(self._scraper, "_hours_interrupted", False)
                    if self._stop_flag or hours_hit or count > 0:
                        break

                # ── Device lost mid-scrape? ──────────────────────────────────
                # If the scraper set _session_dead, the device became
                # unrecoverable (not a normal finish). Break out of the target
                # loop so the worker emits signals.error → triggers auto-restart.
                session_dead = getattr(self._scraper, "_session_dead", False)
                if session_dead and not self._stop_flag:
                    raise RuntimeError(
                        f"Device lost mid-scrape on @{target} "
                        f"(collected {count}/{max_per_target} before session died)"
                    )

                total_collected += count

                # ── Decide whether to advance or re-run this target ──────
                hours_hit = getattr(self._scraper, "_hours_interrupted", False)
                if hours_hit and not self._stop_flag:
                    # Hours ended mid-target. Stay on the same target_idx so
                    # _wait_for_schedule (top of while loop) blocks until the
                    # next window, then re-runs this target from scratch.
                    # Log how many were already collected this partial run.
                    remaining = max_per_target - count
                    self._log(
                        f"⏸️ @{target} paused after {count} accounts "
                        f"(need {remaining} more). Will resume next window."
                    )
                    # Don't advance target_idx — loop back to wait
                else:
                    self._log(f"✅ @{target} done — {count} this run, {total_collected} total")
                    self.signals.target_done.emit(self.phone_index, target)   # notify UI
                    target_idx += 1   # advance to next target

                    if self._stop_flag:
                        break

                    # Rest between targets (skip for the last target)
                    if (target_idx < len(self.targets) and not self._stop_flag):
                        rest_s = random.randint(
                            int(delays.get("rest_min_seconds", 60)),
                            int(delays.get("rest_max_seconds", 300)),
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

    @staticmethod
    def _schedule_duration(schedule: dict):
        """Return the timedelta duration of the configured window."""
        from datetime import timedelta
        start_t = dtime(schedule["start_hour"], schedule["start_minute"])
        end_t   = dtime(schedule["end_hour"],   schedule["end_minute"])
        if end_t > start_t:
            return timedelta(hours=end_t.hour - start_t.hour,
                             minutes=end_t.minute - start_t.minute)
        return timedelta(days=1) - timedelta(hours=start_t.hour - end_t.hour,
                                             minutes=start_t.minute - end_t.minute)

    @staticmethod
    def _next_window(schedule: dict):
        """
        Return the next FUTURE (win_start, win_end) datetimes.
        start_t has already passed today → win_start is tomorrow.
        """
        from datetime import timedelta
        now     = datetime.now()
        start_t = dtime(schedule["start_hour"], schedule["start_minute"])
        win_start = now.replace(hour=start_t.hour, minute=start_t.minute,
                                second=0, microsecond=0)
        if win_start <= now:
            win_start += timedelta(days=1)
        duration = PhoneWorker._schedule_duration(schedule)
        return win_start, win_start + duration

    def _in_schedule_window(self, schedule: dict) -> bool:
        """
        Return True only if we are inside a window that:
          1. Started AFTER the schedule was saved (saved_at), and
          2. Has not yet ended.

        Using saved_at as the anchor prevents a just-passed start from
        being treated as "active" when the user configured it after the
        fact.
        """
        from datetime import timedelta
        now     = datetime.now()
        start_t = dtime(schedule["start_hour"], schedule["start_minute"])

        # Most recent past occurrence of start_t
        prev_start = now.replace(hour=start_t.hour, minute=start_t.minute,
                                 second=0, microsecond=0)
        if prev_start > now:
            prev_start -= timedelta(days=1)

        prev_end = prev_start + self._schedule_duration(schedule)

        # Only active if the window started after the config was saved
        saved_at_str = schedule.get("saved_at", "")
        try:
            saved_at = datetime.fromisoformat(saved_at_str)
        except (ValueError, TypeError):
            saved_at = datetime.min   # no saved_at → conservative: never active

        return (prev_start >= saved_at) and (now < prev_end)

    def _wait_for_schedule(self, schedule: dict):
        start_t = dtime(schedule["start_hour"], schedule["start_minute"])
        end_t   = dtime(schedule["end_hour"],   schedule["end_minute"])
        while not self._stop_flag:
            if self._in_schedule_window(schedule):
                return
            win_start, _ = self._next_window(schedule)
            self._log(f"⏰ Outside hours ({start_t:%H:%M}–{end_t:%H:%M}). "
                      f"Next window: {win_start.strftime('%a %I:%M %p')}. Waiting…")
            self._sleep(60)

    def _is_past_schedule_end(self, schedule: dict) -> bool:
        """Return True if the current time is outside the active schedule window."""
        return not self._in_schedule_window(schedule)

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
    """
    Dashboard with dynamic phone slots (1 visible by default, + button adds more up to MAX_PHONES).
    Each slot shows its device row AND its target-username column together, so adding a phone
    always expands both sections in sync.
    """

    # Emitted when a slot is added or removed so MainWindow can (re-)wire signals
    slot_added   = pyqtSignal(int)   # index of newly shown slot
    slot_removed = pyqtSignal(int)   # index of hidden slot

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Dashboard")

        # Public lists kept at MAX_PHONES length (hidden slots still exist in them)
        self.device_rows: List[Tuple] = []   # (combo_dev, combo_acc, lbl_port, lbl_status, btn_view)
        self.target_rows: List[TextEdit] = []
        self.nick_edits:  List[LineEdit] = []        # nickname input per phone
        self.stop_phone_btns: List[PushButton] = []  # individual stop button per phone

        # Per-slot container widgets (one QWidget per slot holding both device row + target col)
        self._slot_widgets: List[QWidget] = []
        self._separators:   list = []   # separator QFrame above each slot > 0

        # How many slots are currently visible
        self._visible_slots: int = 0

        self._build()

    # ── Public API ────────────────────────────────────────────────────────

    def show_slots(self, n: int):
        """Ensure exactly n slots are visible (used when loading config)."""
        n = max(1, min(MAX_PHONES, n))
        while self._visible_slots < n:
            self._add_slot()
        # Never hide slots that may have data — only add

    def active_slot_count(self) -> int:
        return self._visible_slots

    # ── Build ─────────────────────────────────────────────────────────────

    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        left_scroll = ScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setStyleSheet("ScrollArea{border:none;background:transparent;}")

        self._left_inner = QWidget()
        left_lay = QVBoxLayout(self._left_inner)
        _m  = 24 if _sys.platform == "win32" else 40
        _sp = 14 if _sys.platform == "win32" else 24
        left_lay.setContentsMargins(_m, 40, _m, 40)
        left_lay.setSpacing(_sp)
        left_scroll.setWidget(self._left_inner)
        self._left_lay = left_lay
        self._cs = 16 if _sys.platform == "win32" else 24

        # Title
        title_lbl = TitleLabel("Dashboard", self._left_inner)
        title_lbl.setFont(T.title())
        title_lbl.setStyleSheet(
            f"font-size: {_pts(22)}pt; margin-bottom: {_px(12)}px; background: transparent;"
        )
        left_lay.addWidget(title_lbl)

        # ── Phones card ────────────────────────────────────────────────────
        self._dev_card = CardWidget(self._left_inner)
        self._dev_lay  = QVBoxLayout(self._dev_card)
        self._dev_lay.setContentsMargins(self._cs, self._cs, self._cs, self._cs)
        self._dev_lay.setSpacing(0)   # slots manage their own spacing

        hdr_row = QHBoxLayout()
        h1 = StrongBodyLabel("📱 Connected Phones", self._dev_card)
        h1.setFont(T.heading()); h1.setStyleSheet("background: transparent;")
        hdr_row.addWidget(h1)
        hdr_row.addStretch()
        self.btn_refresh = PushButton(FIF.SYNC, "Refresh", self._dev_card)
        self.btn_refresh.setMinimumHeight(_px(34))
        self.btn_refresh.setMinimumWidth(_px(115))
        self.btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        hdr_row.addWidget(self.btn_refresh)
        self._dev_lay.addLayout(hdr_row)
        self._dev_lay.addSpacing(12)

        # Container for the slot rows (inserted before the + button)
        self._slots_container = QVBoxLayout()
        self._slots_container.setSpacing(0)
        self._dev_lay.addLayout(self._slots_container)

        # + / − phone controls — one pair for the whole card
        self._btn_add = PushButton(FIF.ADD, "Add Phone", self._dev_card)
        self._btn_add.setFont(T.button())
        self._btn_add.setMinimumHeight(_px(36))
        self._btn_add.setFixedWidth(_px(130))
        self._btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add.clicked.connect(self._add_slot)

        self._btn_remove_last = PushButton(FIF.REMOVE, "Remove Phone", self._dev_card)
        self._btn_remove_last.setFont(T.button())
        self._btn_remove_last.setMinimumHeight(_px(36))
        self._btn_remove_last.setFixedWidth(_px(150))
        self._btn_remove_last.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_remove_last.setEnabled(False)   # disabled when only 1 slot visible
        self._btn_remove_last.clicked.connect(self._remove_last_slot)

        ctrl_btn_row = QHBoxLayout()
        ctrl_btn_row.setSpacing(10)
        ctrl_btn_row.addWidget(self._btn_add)
        ctrl_btn_row.addWidget(self._btn_remove_last)
        ctrl_btn_row.addStretch()
        self._dev_lay.addSpacing(10)
        self._dev_lay.addLayout(ctrl_btn_row)

        left_lay.addWidget(self._dev_card)

        # ── Targets card ───────────────────────────────────────────────────
        self._tgt_card = CardWidget(self._left_inner)
        self._tgt_lay  = QVBoxLayout(self._tgt_card)
        self._tgt_lay.setContentsMargins(self._cs, self._cs, self._cs, self._cs)
        self._tgt_lay.setSpacing(12 if _sys.platform == "win32" else 16)
        lbl_tgt = StrongBodyLabel("🎯 Targets per Phone", self._tgt_card)
        lbl_tgt.setFont(T.heading()); lbl_tgt.setStyleSheet("background: transparent;")
        self._tgt_lay.addWidget(lbl_tgt)

        self._targets_grid = QHBoxLayout()
        self._targets_grid.setSpacing(12 if _sys.platform == "win32" else 20)
        self._tgt_lay.addLayout(self._targets_grid)
        left_lay.addWidget(self._tgt_card)

        # Pre-build all MAX_PHONES slots (hidden); show slot 0 immediately
        for i in range(MAX_PHONES):
            self._build_slot(i)
        self._add_slot()   # show first slot

        # ── Configuration & Controls ───────────────────────────────────────
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(14 if _sys.platform == "win32" else 24)

        mode_card = CardWidget(self._left_inner)
        mode_lay  = QVBoxLayout(mode_card)
        mode_lay.setContentsMargins(self._cs, self._cs, self._cs, self._cs)
        mode_lay.setSpacing(0)
        lbl_mode = StrongBodyLabel("⚙️ Run Settings", mode_card)
        lbl_mode.setFont(T.heading()); lbl_mode.setStyleSheet("background: transparent;")
        mode_lay.addWidget(lbl_mode)
        mode_lay.addSpacing(16)

        mode_form = QHBoxLayout(); mode_form.setSpacing(0)
        lbl_m = CaptionLabel("Mode:", mode_card); lbl_m.setFont(T.body()); lbl_m.setStyleSheet("background: transparent;")
        mode_form.addWidget(lbl_m); mode_form.addSpacing(6)
        self.combo_mode = ComboBox(mode_card); self.combo_mode.setFont(T.body())
        self.combo_mode.setMinimumHeight(_px(34)); self.combo_mode.addItems(["followers", "following"])
        mode_form.addWidget(self.combo_mode); mode_form.addSpacing(40)
        lbl_mx = CaptionLabel("Max:", mode_card); lbl_mx.setFont(T.body()); lbl_mx.setStyleSheet("background: transparent;")
        mode_form.addWidget(lbl_mx); mode_form.addSpacing(6)
        self.spin_count = SpinBox(mode_card); self.spin_count.setFont(T.body())
        self.spin_count.setMinimumHeight(_px(34)); self.spin_count.setRange(1, 50000); self.spin_count.setValue(100)
        mode_form.addWidget(self.spin_count); mode_form.addStretch(1)
        mode_lay.addLayout(mode_form); mode_lay.addStretch(1)
        bottom_row.addWidget(mode_card, 1)

        sched_card = CardWidget(self._left_inner)
        sched_lay  = QVBoxLayout(sched_card)
        sched_lay.setContentsMargins(self._cs, self._cs, self._cs, self._cs)
        sched_lay.setSpacing(10)
        self.chk_schedule = CheckBox("Working Hours", sched_card)
        self.chk_schedule.setFont(T.heading()); self.chk_schedule.setStyleSheet("background: transparent;")
        sched_lay.addWidget(self.chk_schedule)
        self.lbl_sched_desc = CaptionLabel(
            "Scraping only runs between these times. Outside this window the bot pauses and waits.",
            sched_card,
        )
        self.lbl_sched_desc.setStyleSheet("background: transparent; color: grey;")
        self.lbl_sched_desc.setWordWrap(True)
        sched_lay.addWidget(self.lbl_sched_desc)
        time_row = QHBoxLayout(); time_row.setSpacing(8)
        self._lbl_sched_start = CaptionLabel("Start:", sched_card); self._lbl_sched_start.setStyleSheet("background: transparent;")
        self.time_start = TimeEdit(sched_card); self.time_start.setFont(T.body())
        self.time_start.setMinimumHeight(_px(44)); self.time_start.setDisplayFormat("hh:mm AP")
        self.time_start.setToolTip("Scraping START time (e.g. 09:00 AM)")
        self._lbl_sched_arrow = CaptionLabel("to", sched_card); self._lbl_sched_arrow.setStyleSheet("background: transparent;")
        self._lbl_sched_end = CaptionLabel("End:", sched_card); self._lbl_sched_end.setStyleSheet("background: transparent;")
        self.time_end = TimeEdit(sched_card); self.time_end.setFont(T.body())
        self.time_end.setMinimumHeight(_px(44)); self.time_end.setDisplayFormat("hh:mm AP")
        self.time_end.setToolTip("Scraping END time (e.g. 06:00 PM)")
        time_row.addWidget(self._lbl_sched_start); time_row.addWidget(self.time_start)
        time_row.addWidget(self._lbl_sched_arrow); time_row.addWidget(self._lbl_sched_end)
        time_row.addWidget(self.time_end); time_row.addStretch()
        sched_lay.addLayout(time_row)
        self.lbl_sched_preview = CaptionLabel("", sched_card)
        self.lbl_sched_preview.setStyleSheet("background: transparent; color: #3b82f6;")
        self.lbl_sched_preview.setWordWrap(True)
        sched_lay.addWidget(self.lbl_sched_preview)
        bottom_row.addWidget(sched_card, 1)
        left_lay.addLayout(bottom_row)

        # Session Summary card
        sum_card = CardWidget(self._left_inner)
        sum_lay  = QVBoxLayout(sum_card)
        sum_lay.setContentsMargins(self._cs, self._cs, self._cs, self._cs)
        sum_lay.setSpacing(10)
        lbl_sum = StrongBodyLabel("📋 Session Summary", sum_card)
        lbl_sum.setFont(T.heading()); lbl_sum.setStyleSheet("background: transparent;")
        sum_lay.addWidget(lbl_sum)
        self.lbl_summary_info = CaptionLabel("No sessions run yet.", sum_card)
        self.lbl_summary_info.setFont(T.body())
        self.lbl_summary_info.setStyleSheet("background: transparent; color: #94a3b8;")
        self.lbl_summary_info.setWordWrap(True)
        sum_lay.addWidget(self.lbl_summary_info)
        self.btn_download_summary = PushButton(FIF.DOWNLOAD, "Download Summary (.txt)", sum_card)
        self.btn_download_summary.setFont(T.button())
        self.btn_download_summary.setMinimumHeight(_px(36))
        self.btn_download_summary.setCursor(Qt.CursorShape.PointingHandCursor)
        sum_lay.addWidget(self.btn_download_summary)
        left_lay.addWidget(sum_card)

        # Action buttons
        ctrl_row = QHBoxLayout(); ctrl_row.setSpacing(20)
        self.btn_start = PrimaryPushButton(FIF.PLAY, "START SCRAPING", self._left_inner)
        self.btn_start.setFont(T.button()); self.btn_start.setMinimumHeight(_px(48))
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor); self.btn_start.setEnabled(False)
        self.btn_stop = PushButton(FIF.CLOSE, "STOP ALL", self._left_inner)
        self.btn_stop.setFont(T.button()); self.btn_stop.setMinimumHeight(_px(48))
        self.btn_stop.setEnabled(False); self.btn_stop.setObjectName("btn_stop_danger")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lbl_overall_status = StrongBodyLabel("Ready", self._left_inner)
        self.lbl_overall_status.setFont(T.body())
        self.lbl_overall_status.setStyleSheet("color: #64748b; margin-left: 15px; background: transparent;")
        ctrl_row.addWidget(self.btn_start, 2); ctrl_row.addWidget(self.btn_stop, 1)
        ctrl_row.addWidget(self.lbl_overall_status, 1)
        left_lay.addLayout(ctrl_row)
        left_lay.addStretch(1)

        outer.addWidget(left_scroll, stretch=65)
        outer.addStretch(35)

    # ── Slot construction ─────────────────────────────────────────────────

    def _build_slot(self, i: int):
        """Pre-build slot i (hidden). Appends to device_rows and target_rows."""
        is_win = _sys.platform == "win32"

        # Outer wrapper: holds device row + optional remove link below it
        outer_w = QWidget(self._dev_card)
        outer_w.setStyleSheet("background: transparent;")
        outer_lay = QVBoxLayout(outer_w)
        outer_lay.setContentsMargins(0, 4, 0, 4)
        outer_lay.setSpacing(2)

        # Device row
        dev_w = QWidget(outer_w)
        dev_w.setStyleSheet("background: transparent;")
        dev_row = QHBoxLayout(dev_w)
        dev_row.setContentsMargins(0, 0, 0, 0)
        dev_row.setSpacing(10 if is_win else 16)

        lbl_num = StrongBodyLabel(f"P{i+1}", dev_w)
        lbl_num.setFont(T.body())
        lbl_num.setFixedWidth(28 if is_win else _px(40))
        lbl_num.setStyleSheet("background: transparent;")

        # Nickname input
        nick_edit = LineEdit(dev_w); nick_edit.setFont(T.body())
        nick_edit.setPlaceholderText(f"Phone {i+1}")
        nick_edit.setFixedWidth(90 if is_win else _px(110))
        nick_edit.setMinimumHeight(_px(32))
        nick_edit.setToolTip("Give this phone a nickname")

        combo_dev = ComboBox(dev_w); combo_dev.setFont(T.body())
        combo_dev.setMinimumHeight(_px(36)); combo_dev.setPlaceholderText("Select Device")
        combo_dev.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        combo_dev.setMaximumWidth(220 if is_win else 300)

        combo_acc = ComboBox(dev_w); combo_acc.setFont(T.body())
        combo_acc.setMinimumHeight(_px(36))
        combo_acc.setFixedWidth(120 if is_win else _px(160))
        combo_acc.setPlaceholderText("Accounts")

        lbl_port = CaptionLabel(f":{4723 + i}", dev_w); lbl_port.setFont(T.caption())
        lbl_port.setFixedWidth(48 if is_win else _px(60))
        lbl_port.setStyleSheet("background: transparent;")

        lbl_status = CaptionLabel("\u25cf idle", dev_w); lbl_status.setFont(T.caption())
        lbl_status.setFixedWidth(58 if is_win else _px(75))
        lbl_status.setStyleSheet("background: transparent;")

        btn_view = PushButton("\U0001f441 View", dev_w); btn_view.setFont(T.button())
        btn_view.setMinimumHeight(_px(34))
        btn_view.setFixedWidth(75 if is_win else _px(90))
        btn_view.setCursor(Qt.CursorShape.PointingHandCursor)

        # Individual stop button (hidden until scraping starts)
        btn_stop_phone = PushButton("⏹", dev_w); btn_stop_phone.setFont(T.button())
        btn_stop_phone.setMinimumHeight(_px(34))
        btn_stop_phone.setFixedWidth(36 if is_win else _px(44))
        btn_stop_phone.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_stop_phone.setEnabled(False)
        btn_stop_phone.setStyleSheet(
            "PushButton { background: #ef4444; border: none; color: white; border-radius: 6px; }"
            "PushButton:hover { background: #dc2626; }"
            "PushButton:disabled { background: #475569; color: #94a3b8; }"
        )

        dev_row.addWidget(lbl_num)
        dev_row.addWidget(nick_edit)
        dev_row.addWidget(combo_dev)
        dev_row.addWidget(combo_acc)
        dev_row.addWidget(lbl_port)
        dev_row.addWidget(lbl_status)
        dev_row.addWidget(btn_stop_phone)
        dev_row.addWidget(btn_view)
        dev_row.addStretch()
        outer_lay.addWidget(dev_w)

        # No per-slot remove button — removal handled by the shared card-level button

        # Thin separator above every slot after the first
        if i > 0:
            sep = QFrame(self._dev_card)
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("QFrame { background-color: #2d3f55; border: none; max-height: 1px; }")
            self._separators.append(sep)
            self._slots_container.addWidget(sep)
            sep.hide()
        else:
            self._separators.append(None)

        self._slots_container.addWidget(outer_w)
        outer_w.hide()

        # Target column widget
        tgt_w = QWidget(self._tgt_card)
        tgt_w.setStyleSheet("background: transparent;")
        tgt_col = QVBoxLayout(tgt_w)
        tgt_col.setContentsMargins(0, 0, 0, 0); tgt_col.setSpacing(4)

        lbl_tgt = CaptionLabel(f"Phone {i+1}", tgt_w)
        lbl_tgt.setFont(T.caption()); lbl_tgt.setStyleSheet("background: transparent;")
        # Keep reference so nickname changes update this label live
        nick_edit.textChanged.connect(
            lambda text, lbl=lbl_tgt, idx=i: lbl.setText(text.strip() or f"Phone {idx+1}")
        )

        txt = TextEdit(tgt_w); txt.setFont(T.body())
        txt.setPlaceholderText("username1\nusername2"); txt.setMinimumHeight(_px(140))
        # transparent so it uses the card background without the dark-box artefact
        txt.setStyleSheet("TextEdit { background: transparent; border: 1px solid #334155; border-radius: 10px; padding: 6px 10px; }")

        tgt_col.addWidget(lbl_tgt); tgt_col.addWidget(txt)
        self._targets_grid.addWidget(tgt_w)
        tgt_w.hide()

        # Register
        self.device_rows.append((combo_dev, combo_acc, lbl_port, lbl_status, btn_view))
        self.target_rows.append(txt)
        self.nick_edits.append(nick_edit)
        self.stop_phone_btns.append(btn_stop_phone)
        self._slot_widgets.append((outer_w, tgt_w))


    # ── Slot show/hide ────────────────────────────────────────────────────

    def _add_slot(self):
        """Show the next hidden slot."""
        i = self._visible_slots
        if i >= MAX_PHONES:
            return
        outer_w, tgt_w = self._slot_widgets[i]
        if i < len(self._separators) and self._separators[i]:
            self._separators[i].show()
        outer_w.show(); tgt_w.show()
        self._visible_slots += 1
        self._btn_add.setEnabled(self._visible_slots < MAX_PHONES)
        self._btn_remove_last.setEnabled(self._visible_slots > 1)
        self.slot_added.emit(i)

    def _remove_last_slot(self):
        """Hide the last visible slot (always index _visible_slots - 1)."""
        i = self._visible_slots - 1
        if i < 1:   # never remove slot 0
            return
        outer_w, tgt_w = self._slot_widgets[i]
        if i < len(self._separators) and self._separators[i]:
            self._separators[i].hide()
        outer_w.hide(); tgt_w.hide()
        combo_dev = self.device_rows[i][0]
        combo_dev.blockSignals(True); combo_dev.setCurrentIndex(0); combo_dev.blockSignals(False)
        self.target_rows[i].clear()
        if i < len(self.nick_edits):
            self.nick_edits[i].clear()
        self._visible_slots -= 1
        self._btn_add.setEnabled(True)
        self._btn_remove_last.setEnabled(self._visible_slots > 1)
        self.slot_removed.emit(i)


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
        self.chk_skip_no_contact.setChecked(False)
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
        lbl_bl = StrongBodyLabel("🏴 Blacklists", bl_card)
        lbl_bl.setFont(T.heading()); lbl_bl.setStyleSheet("background: transparent;")
        bl_lay.addWidget(lbl_bl)
        bl_lay.addWidget(CaptionLabel(
            "Two separate blacklist files — one for normal scrapes, one for keyword-mode scrapes. "
            "They never mix.", bl_card
        ))

        # ── Side-by-side symmetric panels ─────────────────────────────────
        panels_row = QHBoxLayout(); panels_row.setSpacing(20)

        def _bl_panel(title, desc, icon, btn_dl_label, parent):
            """Return (panel_widget, lbl_count, btn_dl, btn_import, btn_clear)."""
            panel = QWidget(parent); panel.setStyleSheet("background: transparent;")
            lay   = QVBoxLayout(panel); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(8)
            lbl_title = StrongBodyLabel(title, panel)
            lbl_title.setFont(T.body()); lbl_title.setStyleSheet("background: transparent;")
            lay.addWidget(lbl_title)
            lbl_desc = CaptionLabel(desc, panel)
            lbl_desc.setStyleSheet("background: transparent; color: grey;"); lbl_desc.setWordWrap(True)
            lay.addWidget(lbl_desc)
            lbl_count = CaptionLabel("0 entries", panel)
            lbl_count.setStyleSheet("background: transparent; color: grey;")
            lbl_count.setFont(T.mono())
            lay.addWidget(lbl_count)
            btn_dl  = PrimaryPushButton(FIF.DOWNLOAD, btn_dl_label, panel)
            btn_imp = PushButton(FIF.FOLDER,  "Import .txt", panel)
            btn_clr = PushButton(FIF.DELETE,  "Clear",       panel)
            for b in [btn_dl, btn_imp, btn_clr]:
                b.setFont(T.button()); b.setMinimumHeight(_px(36)); lay.addWidget(b)
            lay.addStretch()
            return panel, lbl_count, btn_dl, btn_imp, btn_clr

        left_panel, self.lbl_bl_count, self.btn_download_bl, self.btn_import_bl, self.btn_clear_bl = \
            _bl_panel("📋 Main Blacklist",
                      "Usernames already scraped in normal mode. Never scraped again.",
                      FIF.DOWNLOAD, "Download blacklist.txt", bl_card)

        right_panel, self.lbl_kw_bl_count, self.btn_download_kw_bl, self.btn_import_kw_bl, self.btn_clear_kw_bl = \
            _bl_panel("🔑 Keyword-Mode Blacklist",
                      "Usernames scraped via keyword search. Separate from main blacklist.",
                      FIF.DOWNLOAD, "Download blacklist_keyword.txt", bl_card)

        panels_row.addWidget(left_panel, 1)
        # Thin vertical separator
        sep = QFrame(bl_card); sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: #334155; background: #334155;"); sep.setFixedWidth(1)
        panels_row.addWidget(sep)
        panels_row.addWidget(right_panel, 1)
        bl_lay.addLayout(panels_row)

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
        for i in range(MAX_PHONES):
            lbl = CaptionLabel(f"Phone {i+1}: idle", prog_card)
            lbl.setFont(T.body())
            lbl.setStyleSheet("color: #64748b; font-weight: 500; background: transparent;")
            lbl.setVisible(False)   # hidden by default — shown only for active slots
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

        # Activity logs — two panels side by side
        logs_row = QHBoxLayout()
        logs_row.setSpacing(12)

        # Left: Scraping log
        scrape_log_col = QVBoxLayout()
        lbl_log = StrongBodyLabel("📜 Scraping Log", left_inner)
        lbl_log.setFont(T.heading()); lbl_log.setStyleSheet("background: transparent;")
        scrape_log_col.addWidget(lbl_log)
        self.log_area = TextEdit(left_inner)
        self.log_area.setReadOnly(True)
        self.log_area.setMinimumHeight(_px(250))
        self.log_area.setFont(T.mono())
        scrape_log_col.addWidget(self.log_area)
        logs_row.addLayout(scrape_log_col, stretch=1)

        # Right: Main Account log
        ma_log_col = QVBoxLayout()
        lbl_ma_log = StrongBodyLabel("🌟 Main Account Log", left_inner)
        lbl_ma_log.setFont(T.heading()); lbl_ma_log.setStyleSheet("background: transparent;")
        ma_log_col.addWidget(lbl_ma_log)
        self.ma_log_area = TextEdit(left_inner)
        self.ma_log_area.setReadOnly(True)
        self.ma_log_area.setMinimumHeight(_px(250))
        self.ma_log_area.setFont(T.mono())
        ma_log_col.addWidget(self.ma_log_area)
        logs_row.addLayout(ma_log_col, stretch=1)

        left_lay.addLayout(logs_row)

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

        # Enable/disable toggle
        self.chk_enable_sheets = CheckBox("Enable Google Sheets export", sh_card)
        self.chk_enable_sheets.setFont(T.body())
        self.chk_enable_sheets.setStyleSheet("background: transparent;")
        self.chk_enable_sheets.setChecked(True)
        sh_lay.addWidget(self.chk_enable_sheets)

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

        # Keep a list of all sheet-detail widgets so we can en/disable them together
        self._sheet_detail_widgets = [
            self.inp_sheet_id, self.inp_sheet_tab, self.inp_creds,
            self.btn_browse_creds, self.btn_test_sheets, self.btn_revoke_token,
        ]

        def _on_sheets_toggle(state):
            enabled = self.chk_enable_sheets.isChecked()
            for w in self._sheet_detail_widgets:
                w.setEnabled(enabled)
            if not enabled:
                self.lbl_sheet_status.setText("Disabled")
            else:
                self.lbl_sheet_status.setText("Not connected")

        self.chk_enable_sheets.stateChanged.connect(_on_sheets_toggle)
        # Apply initial state
        _on_sheets_toggle(self.chk_enable_sheets.isChecked())

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

        add_delay("Between profiles (s):", "sp_prof_min", "sp_prof_max", 1.0, 300.0)
        add_delay("Between scrolls (s):",  "sp_scrl_min", "sp_scrl_max", 0.5, 300.0)
        add_delay("Rest between runs (s):", "sp_rest_min", "sp_rest_max", 1, 86400, True)

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

        # ── IP Rotation ───────────────────────────────────────────────────
        ip_card = CardWidget(self); ip_lay = QVBoxLayout(ip_card)
        ip_lay.setContentsMargins(30, 30, 30, 30); ip_lay.setSpacing(16)
        lbl_ip = StrongBodyLabel("🔄 IP Rotation (Mobile Data)", ip_card)
        lbl_ip.setFont(T.heading()); lbl_ip.setStyleSheet("background: transparent;")
        ip_lay.addWidget(lbl_ip)
        ip_lay.addWidget(CaptionLabel(
            "Rotates IP by toggling mobile data OFF → ON. "
            "Requires a real SIM card — does NOT work on Wi-Fi or emulators.", ip_card
        ))

        self.chk_ip_enabled = CheckBox("Enable IP rotation", ip_card)
        self.chk_ip_enabled.setFont(T.body()); self.chk_ip_enabled.setStyleSheet("background: transparent;")
        ip_lay.addWidget(self.chk_ip_enabled)

        ip_interval_row = QHBoxLayout()
        lbl_ip_min = CaptionLabel("Rotate every MIN:", ip_card); lbl_ip_min.setFont(T.body()); lbl_ip_min.setStyleSheet("background: transparent;")
        self.sp_ip_min = DoubleSpinBox(ip_card); self.sp_ip_min.setRange(1.0, 120.0); self.sp_ip_min.setValue(5.0)
        self.sp_ip_min.setFont(T.body()); self.sp_ip_min.setMinimumHeight(_px(34)); self.sp_ip_min.setFixedWidth(_px(120))
        lbl_ip_max = CaptionLabel("MAX (minutes):", ip_card); lbl_ip_max.setFont(T.body()); lbl_ip_max.setStyleSheet("background: transparent;")
        self.sp_ip_max = DoubleSpinBox(ip_card); self.sp_ip_max.setRange(1.0, 240.0); self.sp_ip_max.setValue(15.0)
        self.sp_ip_max.setFont(T.body()); self.sp_ip_max.setMinimumHeight(_px(34)); self.sp_ip_max.setFixedWidth(_px(120))
        ip_interval_row.addWidget(lbl_ip_min); ip_interval_row.addWidget(self.sp_ip_min)
        ip_interval_row.addSpacing(24)
        ip_interval_row.addWidget(lbl_ip_max); ip_interval_row.addWidget(self.sp_ip_max)
        ip_interval_row.addStretch()
        ip_lay.addLayout(ip_interval_row)

        def _on_ip_toggle():
            on = self.chk_ip_enabled.isChecked()
            self.sp_ip_min.setEnabled(on); self.sp_ip_max.setEnabled(on)
        self.chk_ip_enabled.stateChanged.connect(lambda _: _on_ip_toggle())
        _on_ip_toggle()
        ip_card.setVisible(False)  # IP Rotation hidden from UI (backend retained)
        self.add(ip_card)

        self.stretch()


# ─────────────────────────────────────────────────────────────────────────────
# MainAccountPage  — story/feed engagement for one designated phone
# ─────────────────────────────────────────────────────────────────────────────

class MainAccountPage(PageWidget):
    """
    Configuration and control panel for the 'Main Account' engagement mode.
    One phone slot can be designated as the Main Account: it watches stories
    (reacting / liking / replying) and browses the feed (liking posts) instead
    of scraping followers.
    """
    def __init__(self, parent=None):
        super().__init__("Main Account", parent)
        self._build()

    def _build(self):
        _cs = 24

        def _lbl(text, parent):
            l = CaptionLabel(text, parent); l.setFont(T.body()); l.setStyleSheet("background:transparent;")
            return l

        def _spin(parent, lo, hi, val, w=150, double=False):
            s = (DoubleSpinBox if double else SpinBox)(parent)
            s.setRange(lo, hi); s.setValue(val)
            s.setFont(T.body()); s.setMinimumHeight(_px(34)); s.setMinimumWidth(_px(w))
            return s

        def _row(*widgets):
            r = QHBoxLayout()
            for w in widgets:
                if isinstance(w, int):
                    r.addSpacing(w)
                else:
                    r.addWidget(w)
            r.addStretch()
            return r

        def _engage_row(parent, label_text, chk_attr, pct_attr, default_pct):
            """Return (layout, checkbox, spinbox) for one engagement action row."""
            chk = CheckBox(label_text, parent)
            chk.setFont(T.body()); chk.setStyleSheet("background:transparent;")
            lbl = _lbl("%:", parent)
            sp  = _spin(parent, 0, 100, default_pct, w=110)
            sp.setSuffix("%")
            row = QHBoxLayout()
            row.addWidget(chk); row.addSpacing(16); row.addWidget(lbl); row.addWidget(sp); row.addStretch()
            return row, chk, sp

        # ── Enable + Phone slot ───────────────────────────────────────────
        en_card = CardWidget(self); en_lay = QVBoxLayout(en_card)
        en_lay.setContentsMargins(_cs, _cs, _cs, _cs); en_lay.setSpacing(16)
        lbl_en = StrongBodyLabel("🌟 Main Account Mode", en_card)
        lbl_en.setFont(T.heading()); lbl_en.setStyleSheet("background:transparent;")
        en_lay.addWidget(lbl_en)
        en_lay.addWidget(CaptionLabel(
            "Designate one phone slot as the Main Account. "
            "It will engage with stories, feed and reels instead of scraping.", en_card
        ))
        slot_row = QHBoxLayout()
        self.chk_ma_enabled = CheckBox("Enable Main Account", en_card)
        self.chk_ma_enabled.setFont(T.body()); self.chk_ma_enabled.setStyleSheet("background:transparent;")
        slot_row.addWidget(self.chk_ma_enabled); slot_row.addSpacing(30)
        lbl_slot = _lbl("Phone slot:", en_card)
        self.combo_ma_slot = ComboBox(en_card); self.combo_ma_slot.setFont(T.body())
        self.combo_ma_slot.setMinimumHeight(_px(36)); self.combo_ma_slot.setFixedWidth(_px(140))
        for i in range(MAX_PHONES):
            self.combo_ma_slot.addItem(f"Phone {i+1}", userData=i)
        slot_row.addWidget(lbl_slot); slot_row.addWidget(self.combo_ma_slot); slot_row.addStretch()
        en_lay.addLayout(slot_row)
        self.add(en_card)

        # ── Working hours ─────────────────────────────────────────────────
        wh_card = CardWidget(self); wh_lay = QVBoxLayout(wh_card)
        wh_lay.setContentsMargins(_cs, _cs, _cs, _cs); wh_lay.setSpacing(12)
        lbl_wh = StrongBodyLabel("⏰ Working Hours Windows", wh_card)
        lbl_wh.setFont(T.heading()); lbl_wh.setStyleSheet("background:transparent;")
        wh_lay.addWidget(lbl_wh)
        wh_lay.addWidget(CaptionLabel(
            "Bot runs during all configured windows. If started mid-window it begins immediately. "
            "Leave empty to run 24/7.", wh_card
        ))
        self.ma_windows: List[Tuple] = []
        self._wh_container = QVBoxLayout()
        wh_lay.addLayout(self._wh_container)

        # Single Add + Remove row (matches phone working hours style)
        wh_btn_row = QHBoxLayout()
        btn_add_window = PushButton(FIF.ADD, "Add Window", wh_card)
        btn_add_window.setFont(T.button()); btn_add_window.setMinimumHeight(_px(36))
        btn_add_window.clicked.connect(self._add_wh_window)
        self._btn_rm_wh = PushButton(FIF.DELETE, "Remove Last", wh_card)
        self._btn_rm_wh.setFont(T.button()); self._btn_rm_wh.setMinimumHeight(_px(36))
        self._btn_rm_wh.clicked.connect(self._remove_last_wh_window)
        wh_btn_row.addWidget(btn_add_window)
        wh_btn_row.addWidget(self._btn_rm_wh)
        wh_btn_row.addStretch()
        wh_lay.addLayout(wh_btn_row)
        self.add(wh_card)
        self._wh_card = wh_card

        # ── Daily time limit ──────────────────────────────────────────────
        dl_card = CardWidget(self); dl_lay = QVBoxLayout(dl_card)
        dl_lay.setContentsMargins(_cs, _cs, _cs, _cs); dl_lay.setSpacing(12)
        lbl_dl = StrongBodyLabel("⏱ Daily Time Limit", dl_card)
        lbl_dl.setFont(T.heading()); lbl_dl.setStyleSheet("background:transparent;")
        dl_lay.addWidget(lbl_dl)
        dl_lay.addWidget(CaptionLabel(
            "Maximum total engagement time per day. Resets at midnight. "
            "Idle/waiting time between windows does NOT count. Disable to run unlimited.",
            dl_card,
        ))

        self.chk_dl_enabled = CheckBox("Enable daily limit", dl_card)
        self.chk_dl_enabled.setFont(T.body())
        self.chk_dl_enabled.setStyleSheet("background:transparent;")
        dl_lay.addWidget(self.chk_dl_enabled)

        dl_row = QHBoxLayout()
        lbl_dl_h = CaptionLabel("Hours:", dl_card); lbl_dl_h.setFont(T.body())
        lbl_dl_h.setStyleSheet("background:transparent;")
        self.sp_dl_hours = SpinBox(dl_card)
        self.sp_dl_hours.setRange(0, 23); self.sp_dl_hours.setValue(4)
        self.sp_dl_hours.setFont(T.body()); self.sp_dl_hours.setMinimumHeight(_px(34))
        self.sp_dl_hours.setFixedWidth(_px(120))

        lbl_dl_m = CaptionLabel("Minutes:", dl_card); lbl_dl_m.setFont(T.body())
        lbl_dl_m.setStyleSheet("background:transparent;")
        self.sp_dl_minutes = SpinBox(dl_card)
        self.sp_dl_minutes.setRange(0, 59); self.sp_dl_minutes.setValue(0)
        self.sp_dl_minutes.setFont(T.body()); self.sp_dl_minutes.setMinimumHeight(_px(34))
        self.sp_dl_minutes.setFixedWidth(_px(120))

        dl_row.addWidget(lbl_dl_h); dl_row.addWidget(self.sp_dl_hours)
        dl_row.addSpacing(24)
        dl_row.addWidget(lbl_dl_m); dl_row.addWidget(self.sp_dl_minutes)
        dl_row.addStretch()
        dl_lay.addLayout(dl_row)

        def _on_dl_toggle():
            on = self.chk_dl_enabled.isChecked()
            self.sp_dl_hours.setEnabled(on)
            self.sp_dl_minutes.setEnabled(on)
        self.chk_dl_enabled.stateChanged.connect(lambda _: _on_dl_toggle())
        _on_dl_toggle()
        self.add(dl_card)

        # ── Stories config ────────────────────────────────────────────────
        st_card = CardWidget(self); st_lay = QVBoxLayout(st_card)
        st_lay.setContentsMargins(_cs, _cs, _cs, _cs); st_lay.setSpacing(10)
        lbl_st = StrongBodyLabel("📖 Stories Engagement", st_card)
        lbl_st.setFont(T.heading()); lbl_st.setStyleSheet("background:transparent;")
        st_lay.addWidget(lbl_st)

        self.chk_st_enabled = CheckBox("Enable story engagement", st_card)
        self.chk_st_enabled.setFont(T.body()); self.chk_st_enabled.setStyleSheet("background:transparent;")
        self.chk_st_enabled.setChecked(True)
        st_lay.addWidget(self.chk_st_enabled)

        # Stories per micro-cycle
        st_lay.addWidget(_lbl("Stories to watch per burst (MIN / MAX):", st_card))
        self.sp_st_watch_min = _spin(st_card, 1, 50, 3)
        self.sp_st_watch_max = _spin(st_card, 1, 100, 7)
        st_lay.addLayout(_row(_lbl("MIN:", st_card), self.sp_st_watch_min,
                              20, _lbl("MAX:", st_card), self.sp_st_watch_max))

        # Seconds per story
        st_lay.addWidget(_lbl("Seconds to watch each story (MIN / MAX):", st_card))
        self.sp_st_wsec_min = _spin(st_card, 1.0, 60.0, 3.0, double=True)
        self.sp_st_wsec_max = _spin(st_card, 1.0, 120.0, 8.0, double=True)
        st_lay.addLayout(_row(_lbl("MIN:", st_card), self.sp_st_wsec_min,
                              20, _lbl("MAX:", st_card), self.sp_st_wsec_max))

        # Short rest
        st_lay.addWidget(_lbl("Short rest between bursts — seconds (MIN / MAX):", st_card))
        self.sp_st_rest_s_min = _spin(st_card, 1.0, 600.0, 10.0, double=True)
        self.sp_st_rest_s_max = _spin(st_card, 1.0, 600.0, 30.0, double=True)
        st_lay.addLayout(_row(_lbl("MIN:", st_card), self.sp_st_rest_s_min,
                              20, _lbl("MAX:", st_card), self.sp_st_rest_s_max))

        # Short cycles before long rest
        st_lay.addWidget(_lbl("Number of bursts before long rest (MIN / MAX):", st_card))
        self.sp_st_cycles_min = _spin(st_card, 1, 50, 3)
        self.sp_st_cycles_max = _spin(st_card, 1, 100, 6)
        st_lay.addLayout(_row(_lbl("MIN:", st_card), self.sp_st_cycles_min,
                              20, _lbl("MAX:", st_card), self.sp_st_cycles_max))

        # Long rest
        st_lay.addWidget(_lbl("Long rest after all bursts — seconds (MIN / MAX):", st_card))
        self.sp_st_rest_l_min = _spin(st_card, 10.0, 3600.0, 120.0, double=True)
        self.sp_st_rest_l_max = _spin(st_card, 10.0, 3600.0, 300.0, double=True)
        st_lay.addLayout(_row(_lbl("MIN:", st_card), self.sp_st_rest_l_min,
                              20, _lbl("MAX:", st_card), self.sp_st_rest_l_max))

        # Engagement actions
        st_lay.addWidget(_lbl("Engagement actions (enable toggle + % chance per story):", st_card))
        r1, self.chk_st_like,    self.sp_st_like_pct    = _engage_row(st_card, "Like",    "", "", 30)
        r2, self.chk_st_react,   self.sp_st_react_pct   = _engage_row(st_card, "React (😮❤️👏🔥)", "", "", 20)
        r3, self.chk_st_comment, self.sp_st_comment_pct = _engage_row(st_card, "Comment", "", "", 10)
        self.chk_st_like.setChecked(True); self.chk_st_react.setChecked(True)
        st_lay.addLayout(r1); st_lay.addLayout(r2); st_lay.addLayout(r3)
        self.add(st_card)

        # ── Feed config ───────────────────────────────────────────────────
        fd_card = CardWidget(self); fd_lay = QVBoxLayout(fd_card)
        fd_lay.setContentsMargins(_cs, _cs, _cs, _cs); fd_lay.setSpacing(10)
        lbl_fd = StrongBodyLabel("📰 Feed Engagement", fd_card)
        lbl_fd.setFont(T.heading()); lbl_fd.setStyleSheet("background:transparent;")
        fd_lay.addWidget(lbl_fd)

        self.chk_fd_enabled = CheckBox("Enable feed engagement", fd_card)
        self.chk_fd_enabled.setFont(T.body()); self.chk_fd_enabled.setStyleSheet("background:transparent;")
        fd_lay.addWidget(self.chk_fd_enabled)

        fd_lay.addWidget(_lbl("Scroll delay — seconds (MIN / MAX):", fd_card))
        self.sp_fd_scroll_min = _spin(fd_card, 0.5, 30.0, 1.5, double=True)
        self.sp_fd_scroll_max = _spin(fd_card, 0.5, 60.0, 4.0, double=True)
        fd_lay.addLayout(_row(_lbl("MIN:", fd_card), self.sp_fd_scroll_min,
                              20, _lbl("MAX:", fd_card), self.sp_fd_scroll_max))

        fd_lay.addWidget(_lbl("Scrolls per cycle:", fd_card))
        self.sp_fd_num_scrolls = _spin(fd_card, 1, 200, 10)
        fd_lay.addLayout(_row(_lbl("Scrolls:", fd_card), self.sp_fd_num_scrolls))

        fd_lay.addWidget(_lbl("Engagement actions:", fd_card))
        fr1, self.chk_fd_like,    self.sp_fd_like_pct    = _engage_row(fd_card, "Like",    "", "", 40)
        fr3, self.chk_fd_comment, self.sp_fd_comment_pct = _engage_row(fd_card, "Comment", "", "", 5)
        self.chk_fd_like.setChecked(True)
        fd_lay.addLayout(fr1); fd_lay.addLayout(fr3)
        self.add(fd_card)

        # ── Reels config ──────────────────────────────────────────────────
        rl_card = CardWidget(self); rl_lay = QVBoxLayout(rl_card)
        rl_lay.setContentsMargins(_cs, _cs, _cs, _cs); rl_lay.setSpacing(10)
        lbl_rl = StrongBodyLabel("🎬 Reels Engagement", rl_card)
        lbl_rl.setFont(T.heading()); lbl_rl.setStyleSheet("background:transparent;")
        rl_lay.addWidget(lbl_rl)

        self.chk_rl_enabled = CheckBox("Enable reels engagement", rl_card)
        self.chk_rl_enabled.setFont(T.body()); self.chk_rl_enabled.setStyleSheet("background:transparent;")
        rl_lay.addWidget(self.chk_rl_enabled)

        rl_lay.addWidget(_lbl("Reels per cycle:", rl_card))
        self.sp_rl_num_reels = _spin(rl_card, 1, 200, 10)
        rl_lay.addLayout(_row(_lbl("Reels:", rl_card), self.sp_rl_num_reels))

        rl_lay.addWidget(_lbl("Watch time per reel — seconds (MIN / MAX):", rl_card))
        self.sp_rl_wsec_min = _spin(rl_card, 1.0, 60.0, 5.0, double=True)
        self.sp_rl_wsec_max = _spin(rl_card, 1.0, 120.0, 15.0, double=True)
        rl_lay.addLayout(_row(_lbl("MIN:", rl_card), self.sp_rl_wsec_min,
                              20, _lbl("MAX:", rl_card), self.sp_rl_wsec_max))

        rl_lay.addWidget(_lbl("Engagement actions:", rl_card))
        rr1, self.chk_rl_like,    self.sp_rl_like_pct    = _engage_row(rl_card, "Like",    "", "", 30)
        rr3, self.chk_rl_comment, self.sp_rl_comment_pct = _engage_row(rl_card, "Comment", "", "", 5)
        self.chk_rl_like.setChecked(True)
        rl_lay.addLayout(rr1); rl_lay.addLayout(rr3)
        self.add(rl_card)

        # ── Replies / Comments config ─────────────────────────────────────
        rp_card = CardWidget(self); rp_lay = QVBoxLayout(rp_card)
        rp_lay.setContentsMargins(_cs, _cs, _cs, _cs); rp_lay.setSpacing(14)
        lbl_rp = StrongBodyLabel("💬 Comment / Reply Templates", rp_card)
        lbl_rp.setFont(T.heading()); lbl_rp.setStyleSheet("background:transparent;")
        rp_lay.addWidget(lbl_rp)

        lbl_spintax = CaptionLabel(
            "Spintax templates — one per line. Use {option1|option2} syntax.\n"
            "Example:  {Great shot!|Love this 🔥|Amazing content|Keep it up!}",
            rp_card,
        )
        lbl_spintax.setFont(T.caption()); lbl_spintax.setStyleSheet("background:transparent;"); lbl_spintax.setWordWrap(True)
        rp_lay.addWidget(lbl_spintax)
        self.txt_spintax = TextEdit(rp_card); self.txt_spintax.setFont(T.mono())
        self.txt_spintax.setPlaceholderText("{Great!|Amazing 🔥|Love this!}\n{Nice shot|Beautiful}")
        self.txt_spintax.setMinimumHeight(_px(120))
        rp_lay.addWidget(self.txt_spintax)

        _lbl_openai_key = CaptionLabel("OpenAI API Key (leave blank to use spintax only):", rp_card)
        _lbl_openai_key.setVisible(False)   # OpenAI hidden from UI (backend retained)
        rp_lay.addWidget(_lbl_openai_key)
        self.inp_openai_key = LineEdit(rp_card); self.inp_openai_key.setFont(T.body())
        self.inp_openai_key.setMinimumHeight(_px(36)); self.inp_openai_key.setPlaceholderText("sk-…")
        self.inp_openai_key.setEchoMode(LineEdit.EchoMode.Password)
        self.inp_openai_key.setVisible(False)   # OpenAI hidden from UI (backend retained)
        rp_lay.addWidget(self.inp_openai_key)

        _lbl_openai_ctx = CaptionLabel("OpenAI context prompt:", rp_card)
        _lbl_openai_ctx.setVisible(False)   # OpenAI hidden from UI (backend retained)
        rp_lay.addWidget(_lbl_openai_ctx)
        self.inp_openai_context = LineEdit(rp_card); self.inp_openai_context.setFont(T.body())
        self.inp_openai_context.setMinimumHeight(_px(36))
        self.inp_openai_context.setPlaceholderText("Write a short friendly reply to this Instagram story.")
        self.inp_openai_context.setVisible(False)   # OpenAI hidden from UI (backend retained)
        rp_lay.addWidget(self.inp_openai_context)
        self.add(rp_card)

        # ── Start / Stop ──────────────────────────────────────────────────
        ctrl_card = CardWidget(self); ctrl_lay = QHBoxLayout(ctrl_card)
        ctrl_lay.setContentsMargins(_cs, _cs, _cs, _cs); ctrl_lay.setSpacing(20)
        self.btn_ma_start = PrimaryPushButton(FIF.PLAY, "START MAIN ACCOUNT", ctrl_card)
        self.btn_ma_start.setFont(T.button()); self.btn_ma_start.setMinimumHeight(_px(48))
        self.btn_ma_stop  = PushButton(FIF.CLOSE, "STOP", ctrl_card)
        self.btn_ma_stop.setFont(T.button()); self.btn_ma_stop.setMinimumHeight(_px(48))
        self.btn_ma_stop.setObjectName("btn_stop_danger"); self.btn_ma_stop.setEnabled(False)
        self.lbl_ma_status = StrongBodyLabel("Idle", ctrl_card)
        self.lbl_ma_status.setFont(T.body()); self.lbl_ma_status.setStyleSheet("color:#64748b;margin-left:15px;background:transparent;")
        ctrl_lay.addWidget(self.btn_ma_start, 2); ctrl_lay.addWidget(self.btn_ma_stop, 1)
        ctrl_lay.addWidget(self.lbl_ma_status, 2)
        self.add(ctrl_card)
        self.stretch()

        # ── Wire enable/disable locking for each section ──────────────────
        self._st_lockable = [
            self.sp_st_watch_min, self.sp_st_watch_max,
            self.sp_st_wsec_min,  self.sp_st_wsec_max,
            self.sp_st_rest_s_min,self.sp_st_rest_s_max,
            self.sp_st_cycles_min,self.sp_st_cycles_max,
            self.sp_st_rest_l_min,self.sp_st_rest_l_max,
            self.chk_st_like,     self.sp_st_like_pct,
            self.chk_st_react,    self.sp_st_react_pct,
            self.chk_st_comment,  self.sp_st_comment_pct,
        ]
        self._fd_lockable = [
            self.sp_fd_scroll_min, self.sp_fd_scroll_max,
            self.sp_fd_num_scrolls,
            self.chk_fd_like,      self.sp_fd_like_pct,
            self.chk_fd_comment,   self.sp_fd_comment_pct,
        ]
        self._rl_lockable = [
            self.sp_rl_num_reels,
            self.sp_rl_wsec_min, self.sp_rl_wsec_max,
            self.chk_rl_like,    self.sp_rl_like_pct,
            self.chk_rl_comment, self.sp_rl_comment_pct,
        ]
        # ── Section toggle: enable/disable all widgets in section,
        # but respect each action-checkbox's own spinbox lock state.
        # Spinboxes are only re-enabled if their own checkbox is also checked.
        def _apply_section(section_widgets, section_enabled: bool,
                           chk_sp_pairs=None):
            for w in section_widgets:
                w.setEnabled(section_enabled)
            # After enabling the section, re-apply per-spinbox locks
            if section_enabled and chk_sp_pairs:
                for chk, sp in chk_sp_pairs:
                    sp.setEnabled(chk.isChecked())

        _st_pairs = [
            (self.chk_st_like,    self.sp_st_like_pct),
            (self.chk_st_react,   self.sp_st_react_pct),
            (self.chk_st_comment, self.sp_st_comment_pct),
        ]
        _fd_pairs = [
            (self.chk_fd_like,    self.sp_fd_like_pct),
            (self.chk_fd_comment, self.sp_fd_comment_pct),
        ]
        _rl_pairs = [
            (self.chk_rl_like,    self.sp_rl_like_pct),
            (self.chk_rl_comment, self.sp_rl_comment_pct),
        ]

        self.chk_st_enabled.stateChanged.connect(
            lambda s: _apply_section(self._st_lockable, bool(s), _st_pairs))
        self.chk_fd_enabled.stateChanged.connect(
            lambda s: _apply_section(self._fd_lockable, bool(s), _fd_pairs))
        self.chk_rl_enabled.stateChanged.connect(
            lambda s: _apply_section(self._rl_lockable, bool(s), _rl_pairs))

        # Per-checkbox spinbox locking — only fires when section is enabled
        def _wire_spinbox_lock(chk, sp, section_chk):
            def _update(state):
                if section_chk.isChecked():
                    sp.setEnabled(bool(state))
            chk.stateChanged.connect(_update)

        _wire_spinbox_lock(self.chk_st_like,    self.sp_st_like_pct,    self.chk_st_enabled)
        _wire_spinbox_lock(self.chk_st_react,   self.sp_st_react_pct,   self.chk_st_enabled)
        _wire_spinbox_lock(self.chk_st_comment, self.sp_st_comment_pct, self.chk_st_enabled)
        _wire_spinbox_lock(self.chk_fd_like,    self.sp_fd_like_pct,    self.chk_fd_enabled)
        _wire_spinbox_lock(self.chk_fd_comment, self.sp_fd_comment_pct, self.chk_fd_enabled)
        _wire_spinbox_lock(self.chk_rl_like,    self.sp_rl_like_pct,    self.chk_rl_enabled)
        _wire_spinbox_lock(self.chk_rl_comment, self.sp_rl_comment_pct, self.chk_rl_enabled)

        # ── Main Account master lock — locks everything below chk_ma_enabled
        # Collects all MA sub-widgets except chk_ma_enabled itself.
        self._ma_all_lockable = (
            [self.combo_ma_slot, self.chk_dl_enabled,
             self.sp_dl_hours, self.sp_dl_minutes,
             self._wh_card,
             self.chk_st_enabled, self.chk_fd_enabled, self.chk_rl_enabled,
             self.btn_ma_start] +
            self._st_lockable + self._fd_lockable + self._rl_lockable
        )

        def _apply_ma_master(ma_enabled: bool):
            for w in self._ma_all_lockable:
                w.setEnabled(ma_enabled)
            if ma_enabled:
                # Re-apply sub-section states after master re-enables
                _apply_section(self._st_lockable, self.chk_st_enabled.isChecked(), _st_pairs)
                _apply_section(self._fd_lockable, self.chk_fd_enabled.isChecked(), _fd_pairs)
                _apply_section(self._rl_lockable, self.chk_rl_enabled.isChecked(), _rl_pairs)
                # dl spinboxes follow their own checkbox
                self.sp_dl_hours.setEnabled(self.chk_dl_enabled.isChecked())
                self.sp_dl_minutes.setEnabled(self.chk_dl_enabled.isChecked())

        self.chk_ma_enabled.stateChanged.connect(
            lambda s: _apply_ma_master(bool(s)))

        # ── Apply all initial states ──────────────────────────────────────
        _apply_ma_master(self.chk_ma_enabled.isChecked())

    def _add_wh_window(self, start_h=9, start_m=0, end_h=19, end_m=0):
        """Add one editable working-hour window row."""
        row_widget = QWidget(self._wh_card)
        row_widget.setStyleSheet("background: transparent;")
        row_lay = QHBoxLayout(row_widget)
        row_lay.setContentsMargins(0, 0, 0, 0); row_lay.setSpacing(10)

        te_start = TimeEdit(row_widget); te_start.setDisplayFormat("hh:mm AP")
        te_start.setTime(QTime(start_h, start_m)); te_start.setFont(T.body())
        te_start.setMinimumHeight(_px(44))

        lbl_to = CaptionLabel("→", row_widget); lbl_to.setStyleSheet("background:transparent;")

        te_end = TimeEdit(row_widget); te_end.setDisplayFormat("hh:mm AP")
        te_end.setTime(QTime(end_h, end_m)); te_end.setFont(T.body())
        te_end.setMinimumHeight(_px(44))

        row_lay.addWidget(te_start); row_lay.addWidget(lbl_to); row_lay.addWidget(te_end)
        row_lay.addStretch()

        entry = (te_start, te_end, row_widget)
        self.ma_windows.append(entry)
        self._wh_container.addWidget(row_widget)

    def _remove_last_wh_window(self):
        """Remove the last working-hour window row."""
        if not self.ma_windows:
            return
        *keep, last = self.ma_windows
        self.ma_windows[:] = keep
        last[-1].setParent(None)
        last[-1].deleteLater()

    def get_windows(self) -> list:
        """Return list of window dicts for config serialisation."""
        result = []
        for te_start, te_end, _ in self.ma_windows:
            ts = te_start.time(); te = te_end.time()
            result.append({
                "start_hour": ts.hour(), "start_minute": ts.minute(),
                "end_hour":   te.hour(), "end_minute":   te.minute(),
            })
        return result

    def load_windows(self, windows: list):
        """Rebuild window rows from a saved config list."""
        for _, _, w in list(self.ma_windows):
            w.setParent(None); w.deleteLater()
        self.ma_windows.clear()
        for win in windows:
            self._add_wh_window(
                start_h=int(win.get("start_hour", 9)),
                start_m=int(win.get("start_minute", 0)),
                end_h=int(win.get("end_hour", 19)),
                end_m=int(win.get("end_minute", 0)),
            )


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

        # Style tooltips here so it works regardless of entry point
        QApplication.instance().setStyleSheet(
            QApplication.instance().styleSheet() +
            " QToolTip {"
            "  background-color: #1e293b;"
            "  color: #f1f5f9;"
            "  border: 1px solid #475569;"
            "  padding: 4px 8px;"
            "  border-radius: 4px;"
            "  font-size: 12px;"
            "}"
        )

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
        self._ma_worker         = None   # MainAccountWorker instance (or None)
        self._cached_devices:   list = []  # last known [(serial, model), ...]

        self.setWindowTitle("Cansa")
        self.resize(1600, 1000)

        self.dashboard_page    = DashboardPage(self)
        self.filters_page      = FiltersPage(self)
        self.results_page      = ResultsPage(self)
        self.settings_page     = SettingsPage(self)
        self.main_account_page = MainAccountPage(self)

        self._init_persistent_mirror()
        self._init_nav()
        self._init_theme()
        self._load_cfg_into_ui()
        self._connect_signals()
        self._refresh_devices()
        self._sync_ma_slot_combo()     # ensure MA combo matches visible slots
        self._sync_ma_target_lock()    # lock MA-assigned target box on startup
        self._reload_blacklist_ui()
        self._on_schedule_toggled()   # apply enabled/disabled state on load
        self._setup_network_monitor() # start internet connectivity watcher

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
        self.addSubInterface(self.dashboard_page,    FIF.HOME,      "Dashboard")
        self.addSubInterface(self.filters_page,      FIF.FILTER,    "Filters & Blacklist")
        self.addSubInterface(self.results_page,      FIF.COMPLETED, "Results")
        self.addSubInterface(self.main_account_page, FIF.HEART,     "Main Account")
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
                padding: 2px 8px; font-size: 10pt;
                min-width: 110px; max-width: 140px; min-height: 38px;
                selection-background-color: transparent; selection-color: {text};
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
                 self.results_page,   self.settings_page,
                 self.main_account_page]

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
                    # TimeEdit also inherits QAbstractSpinBox — give it more height
                    from qfluentwidgets import TimeEdit as _TE
                    if isinstance(w, _TE):
                        w._scale_base_h = _px(44)
                    else:
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
                    from qfluentwidgets import TimeEdit as _TE2
                    if not isinstance(w, _TE2):
                        # Regular spinboxes stay fixed-height
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
        dp.btn_download_summary.clicked.connect(self._download_summary)
        dp.chk_schedule.stateChanged.connect(self._on_schedule_toggled)
        dp.time_start.timeChanged.connect(self._update_schedule_preview)
        dp.time_end.timeChanged.connect(self._update_schedule_preview)

        # Wire already-visible slots and any future slots added via +
        for i in range(dp.active_slot_count()):
            self._wire_slot(i)
        dp.slot_added.connect(self._wire_slot)
        dp.slot_added.connect(self._sync_ma_slot_combo)
        dp.slot_removed.connect(self._sync_ma_slot_combo)

        sp.btn_browse_creds.clicked.connect(self._browse_credentials)
        sp.btn_test_sheets.clicked.connect(self._test_sheets)
        sp.btn_revoke_token.clicked.connect(self._revoke_token)
        fp.btn_download_bl.clicked.connect(self._download_blacklist_txt)
        fp.btn_import_bl.clicked.connect(self._import_blacklist_txt)
        fp.btn_clear_bl.clicked.connect(self._clear_blacklist)
        fp.btn_download_kw_bl.clicked.connect(self._download_kw_blacklist_txt)
        fp.btn_import_kw_bl.clicked.connect(self._import_kw_blacklist_txt)
        fp.btn_clear_kw_bl.clicked.connect(self._clear_kw_blacklist)
        rp.btn_export_csv.clicked.connect(self._export_csv)

        # Main Account page
        mp = self.main_account_page
        mp.btn_ma_start.clicked.connect(self._start_main_account)
        mp.btn_ma_stop.clicked.connect(self._stop_main_account)

        # When the MA-enabled checkbox or the assigned phone slot changes,
        # update which target TextEdit is disabled (can't scrape and be MA at once).
        mp.chk_ma_enabled.stateChanged.connect(self._sync_ma_target_lock)
        mp.combo_ma_slot.currentIndexChanged.connect(self._sync_ma_target_lock)

    def _sync_ma_target_lock(self, _=None):
        """Disable the target TextEdit for whichever slot is assigned as Main Account
        (when MA is enabled). All other slots remain editable. Called whenever
        chk_ma_enabled or combo_ma_slot changes — no thread concerns because both
        are pure UI signals that always fire on the main thread."""
        mp  = self.main_account_page
        dp  = self.dashboard_page
        ma_enabled = mp.chk_ma_enabled.isChecked()
        ma_slot    = mp.combo_ma_slot.currentData()
        if ma_slot is None:
            ma_slot = 0
        for i, txt in enumerate(dp.target_rows):
            locked = ma_enabled and (i == ma_slot)
            txt.setEnabled(not locked)
            if locked:
                txt.setPlaceholderText("Assigned to Main Account — scraping disabled")
                txt.setToolTip("This phone is the Main Account; it cannot scrape targets.")
            else:
                txt.setPlaceholderText("username1\nusername2")

    def _sync_ma_slot_combo(self, _slot_idx=None):
        """Rebuild combo_ma_slot to match only the currently visible dashboard slots.
        Preserves the previously selected slot index where possible. Safe to call
        from slot_added / slot_removed signals (always main-thread Qt signals)."""
        mp  = self.main_account_page
        dp  = self.dashboard_page
        prev = mp.combo_ma_slot.currentData()
        mp.combo_ma_slot.blockSignals(True)
        mp.combo_ma_slot.clear()
        n = dp.active_slot_count()
        for i in range(n):
            mp.combo_ma_slot.addItem(self._phone_label(i), userData=i)
        # Restore selection; fall back to 0 if the previously selected slot
        # was removed (visible slots shrank below it).
        idx = mp.combo_ma_slot.findData(prev)
        mp.combo_ma_slot.setCurrentIndex(idx if idx >= 0 else 0)
        mp.combo_ma_slot.blockSignals(False)
        # Re-evaluate which target box to lock now that slot count may have changed
        self._sync_ma_target_lock()

    def _wire_slot(self, i: int):
        """Connect signals for device row slot i and populate combo_dev from cache.

        Guards against double-connection: each widget only gets one connection
        per signal, tracked via a custom attribute so re-entrant calls (e.g.
        slot_added firing during init for an already-wired slot) are safe.
        """
        dp = self.dashboard_page
        if i >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[i]

        # Populate combo_dev with the last-known device list so the new slot
        # is immediately usable without requiring a manual Refresh click.
        if self._cached_devices and combo_dev.count() == 0:
            combo_dev.blockSignals(True)
            combo_dev.addItem("(not assigned)", userData=None)
            for serial, model in self._cached_devices:
                combo_dev.addItem(f"{model} [{serial}]", userData=serial)
            combo_dev.setCurrentIndex(0)
            combo_dev.blockSignals(False)

        # Only connect once per widget — guard with a set stored on the widget itself
        if not getattr(combo_dev, "_wired", False):
            combo_dev.currentIndexChanged.connect(
                lambda _v, idx=i: self._on_device_selected(idx)
            )
            combo_dev._wired = True

        if not getattr(combo_acc, "_wired", False):
            combo_acc.currentIndexChanged.connect(
                lambda _v, idx=i: self._on_account_selected(idx)
            )
            combo_acc._wired = True

        if not getattr(btn_view, "_wired", False):
            btn_view.clicked.connect(
                lambda _checked=False, idx=i: self._on_view_clicked(idx)
            )
            btn_view._wired = True

        # Wire nick_edit so MA slot combo labels update in real time as the user types.
        if i < len(dp.nick_edits):
            nick_edit = dp.nick_edits[i]
            if not getattr(nick_edit, "_ma_wired", False):
                nick_edit.textChanged.connect(
                    lambda _text, idx=i: self._on_nick_changed(idx)
                )
                nick_edit._ma_wired = True

    # ── Device helpers ────────────────────────────────────────────────────

    def _on_nick_changed(self, slot_idx: int):
        """Called when a phone nickname is edited. Updates the corresponding entry
        in combo_ma_slot in real time without disturbing the current selection.
        Runs on the main thread (Qt signal), so no locking needed."""
        mp = self.main_account_page
        item_idx = mp.combo_ma_slot.findData(slot_idx)
        if item_idx >= 0:
            mp.combo_ma_slot.setItemText(item_idx, self._phone_label(slot_idx))

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

    def _update_schedule_preview(self, _=None):
        """Recompute and show when the next window will actually run."""
        from datetime import datetime, time as dtime, timedelta
        dp = self.dashboard_page
        if not dp.chk_schedule.isChecked():
            dp.lbl_sched_preview.setText("")
            return
        ts = dp.time_start.time()
        te = dp.time_end.time()
        start_t = dtime(ts.hour(), ts.minute())
        end_t   = dtime(te.hour(), te.minute())
        now     = datetime.now()

        if end_t > start_t:
            duration = timedelta(hours=end_t.hour - start_t.hour,
                                 minutes=end_t.minute - start_t.minute)
        else:
            duration = timedelta(days=1) - timedelta(hours=start_t.hour - end_t.hour,
                                                      minutes=start_t.minute - end_t.minute)

        # Most recent past start occurrence
        prev_start = now.replace(hour=start_t.hour, minute=start_t.minute,
                                 second=0, microsecond=0)
        if prev_start > now:
            prev_start -= timedelta(days=1)
        prev_end = prev_start + duration

        # Active only if prev_start >= saved_at AND still inside window
        # For preview purposes saved_at = "right now" (user is editing live)
        # so we show active only if the window genuinely started before now
        # AND would still be running — but since user is editing, treat as
        # "the moment they finish and click Start" = now.
        # We show active if prev_start is recent enough that the window is running.
        # Since we can't know saved_at here, just show the next start always as
        # the honest answer — user decides.
        next_start = now.replace(hour=start_t.hour, minute=start_t.minute,
                                 second=0, microsecond=0)
        if next_start <= now:
            next_start += timedelta(days=1)
        next_end = next_start + duration

        # Show active only if currently inside window AND prev_start is very recent
        # (within last 5 minutes) — meaning the window just started and user likely
        # intended it. Otherwise show next window.
        in_window = (prev_start <= now < prev_end) and                     ((now - prev_start).total_seconds() <= 300)

        if in_window:
            dp.lbl_sched_preview.setText(
                f"▶ Active now — ends {prev_end.strftime('%a %I:%M %p')}"
            )
            dp.lbl_sched_preview.setStyleSheet("background: transparent; color: #22c55e;")
        else:
            dp.lbl_sched_preview.setText(
                f"⏳ Next window: {next_start.strftime('%a %I:%M %p')} → {next_end.strftime('%a %I:%M %p')}"
            )
            dp.lbl_sched_preview.setStyleSheet("background: transparent; color: #f59e0b;")

    def _on_schedule_toggled(self, _state=None):
        """Enable/disable time pickers based on the Working Hours checkbox."""
        dp = self.dashboard_page
        enabled = dp.chk_schedule.isChecked()
        for w in [dp.time_start, dp.time_end,
                  dp._lbl_sched_start, dp._lbl_sched_end, dp._lbl_sched_arrow,
                  dp.lbl_sched_desc, dp.lbl_sched_preview]:
            w.setEnabled(enabled)
        self._update_schedule_preview()

    def _refresh_devices(self):
        devices = get_connected_devices()
        self._cached_devices = devices  # cache for new slots added later
        dp = self.dashboard_page

        # Build a set of serials that are actively running (scraper or MA worker)
        # so we can skip re-detection for them and avoid interrupting live sessions.
        running_serials: set = set()
        for w in self._workers:
            if w.isRunning():
                running_serials.add(w.serial)
        if self._ma_worker and self._ma_worker.isRunning():
            running_serials.add(self._ma_worker.serial)

        # Only refresh currently visible slots
        for i in range(dp.active_slot_count()):
            combo_dev, combo_acc, lbl_port, lbl_status, btn = dp.device_rows[i]

            # Remember which serial was previously selected so we can restore it
            prev_serial = combo_dev.currentData()

            # If this slot's device is actively running, only refresh the device
            # list (so new devices appear) but skip re-detection entirely.
            slot_is_running = prev_serial and prev_serial in running_serials

            combo_dev.blockSignals(True)
            combo_dev.clear()
            combo_dev.addItem("(not assigned)", userData=None)
            for serial, model in devices:
                combo_dev.addItem(f"{model} [{serial}]", userData=serial)

            # Restore previous selection if the device is still connected
            restored = False
            if prev_serial:
                for j in range(combo_dev.count()):
                    if combo_dev.itemData(j) == prev_serial:
                        combo_dev.setCurrentIndex(j)
                        restored = True
                        break

            if not restored:
                combo_dev.setCurrentIndex(0)
                # Device was removed or nothing was selected — reset combo_acc cleanly
                combo_acc.blockSignals(True)
                combo_acc.clear()
                combo_acc.setPlaceholderText("Accounts")
                combo_acc.setEnabled(True)
                combo_acc.blockSignals(False)
                lbl_status.setText("● idle")

            combo_dev.blockSignals(False)

            # Skip detection for slots that are actively running — interrupting
            # an in-progress uiautomator/ADB call on a busy device causes the
            # automation to stall.  The slot already has correct account info.
            if slot_is_running:
                continue

            # Always stagger detection workers across all slots — even across
            # different devices, simultaneous uiautomator calls compete for
            # ADB/accessibility resources and cause timeouts that fall back to
            # "Account 1". 1500 ms per slot: phone 0 starts immediately,
            # phone 1 after 1.5 s, phone 2 after 3 s, phone 3 after 4.5 s.
            if combo_dev.currentData():
                delay_ms = i * 1500
                QTimer.singleShot(delay_ms, lambda idx=i: self._on_device_selected(idx))

        if devices:
            names = ", ".join(model for _, model in devices)
            count = len(devices)
            label = "device" if count == 1 else "devices"
            InfoBar.success(
                title=f"{count} {label} connected",
                content=f"{names} — select a slot to assign.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True, duration=5000, parent=self,
            )
        else:
            InfoBar.warning(
                title="No devices found",
                content="Connect a phone via USB and press Refresh.",
                orient=Qt.Orientation.Horizontal,
                isClosable=True, duration=6000, parent=self,
            )
        self._log(f"🔍 Found {len(devices)} device(s).")

    def _on_device_selected(self, idx: int):
        dp = self.dashboard_page
        if idx >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[idx]
        serial = combo_dev.currentData()

        combo_acc.blockSignals(True)
        combo_acc.clear()
        combo_acc.blockSignals(False)

        if not serial:
            combo_acc.setPlaceholderText("Accounts")
            combo_acc.setEnabled(True)
            lbl_status.setText("● idle")
            # Cancel any in-flight detection for this slot
            old_worker = self._detection_workers.pop(idx, None)
            if old_worker is not None:
                old_worker._cancelled = True
                # Don't terminate() — ADB calls can't be interrupted safely.
                # Setting _cancelled means the finished/error signal will be
                # ignored when it arrives (checked in _on_accounts_detected).
            self._update_start_button_state()
            return

        # Prevent duplicate assignment — if this serial is already selected in
        # another slot, reset the current combo back to "(not assigned)".
        for other_i, (other_combo_dev, *_rest) in enumerate(dp.device_rows):
            if other_i == idx:
                continue
            if other_combo_dev.currentData() == serial:
                combo_dev.blockSignals(True)
                combo_dev.setCurrentIndex(0)
                combo_dev.blockSignals(False)
                combo_acc.setPlaceholderText("Accounts")
                combo_acc.setEnabled(True)
                lbl_status.setText("● idle")
                from qfluentwidgets import InfoBar
                InfoBar.warning(
                    title="Device already assigned",
                    content=f"This device is already assigned to Phone {other_i + 1}.",
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True, duration=4000, parent=self,
                )
                self._update_start_button_state()
                return

        # Cancel previous detection worker for this slot without terminate().
        # terminate() on a thread blocked in a native ADB call is unreliable —
        # the thread may keep running and its queued signal fires after the new
        # worker is already stored, causing two concurrent writes to combo_acc.
        old_worker = self._detection_workers.pop(idx, None)
        if old_worker is not None:
            old_worker._cancelled = True  # signal handler will drop its result

        combo_acc.setPlaceholderText("Detecting…")
        combo_acc.setEnabled(False)
        lbl_status.setText("⏳ detecting")

        worker = AccountDetectionWorker(row_idx=idx, serial=serial)
        worker._cancelled = False
        worker._serial    = serial   # tag so stale signals can be detected
        worker.finished.connect(self._on_accounts_detected)
        worker.error.connect(self._on_accounts_error)
        self._detection_workers[idx] = worker
        worker.start()
        self._update_start_button_state()

    def _on_accounts_detected(self, row_idx: int, accounts: list):
        # Drop result if worker was cancelled (user changed device before ADB finished)
        worker = self._detection_workers.get(row_idx)
        sender = self.sender()
        if sender is not None and getattr(sender, "_cancelled", False):
            # This is a stale result from an old worker — discard silently
            return
        # Also drop if the current worker for this slot isn't the one that emitted
        if worker is not None and sender is not None and sender is not worker:
            return

        dp = self.dashboard_page
        if row_idx >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]

        # Verify the serial still matches what we launched detection for
        current_serial = combo_dev.currentData()
        if sender is not None and getattr(sender, "_serial", None) != current_serial:
            # User changed the combo while ADB was running — result is stale
            return

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
        # Drop result if worker was cancelled
        sender = self.sender()
        if sender is not None and getattr(sender, "_cancelled", False):
            return
        worker = self._detection_workers.get(row_idx)
        if worker is not None and sender is not None and sender is not worker:
            return

        dp = self.dashboard_page
        if row_idx >= len(dp.device_rows):
            return
        combo_dev, combo_acc, lbl_port, lbl_status, btn_view = dp.device_rows[row_idx]

        current_serial = combo_dev.currentData()
        if sender is not None and getattr(sender, "_serial", None) != current_serial:
            return

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
        """Enable START SCRAPING and START MAIN ACCOUNT only when a device is
        assigned and no detection is running."""
        dp = self.dashboard_page
        mp = self.main_account_page
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
        # Lock MA start button during detection — it needs a clean device too
        ma_running = self._ma_worker is not None and self._ma_worker.isRunning()
        if not ma_running:
            mp.btn_ma_start.setEnabled(any_device and not detecting)

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
            InfoBar.warning("No Device", f"Phone {row_idx + 1} has no device assigned.", isClosable=True, duration=6000, parent=self)
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
        self.lbl_mirror_device.setText(f"Mirroring: {self._phone_label(row_idx)}")
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
        cfg["phone_nicknames"] = [
            edit.text().strip() for edit in dp.nick_edits
        ]
        cfg["last_mode"]  = dp.combo_mode.currentText()
        cfg["last_count"] = dp.spin_count.value()

        ts = dp.time_start.time()
        te = dp.time_end.time()
        cfg["schedule"] = {
            "enabled":        dp.chk_schedule.isChecked(),
            "start_hour":     ts.hour(),
            "start_minute":   ts.minute(),
            "end_hour":       te.hour(),
            "end_minute":     te.minute(),
            "saved_at":       datetime.now().isoformat(),

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
        cfg["sheets_enabled"]   = sp.chk_enable_sheets.isChecked()
        cfg["appium"]["host"]   = sp.inp_appium_host.text().strip()
        cfg["delays"] = {
            "between_profiles_min":   sp.sp_prof_min.value(),
            "between_profiles_max":   sp.sp_prof_max.value(),
            "between_scrolls_min":    sp.sp_scrl_min.value(),
            "between_scrolls_max":    sp.sp_scrl_max.value(),
            "rest_min_seconds":       sp.sp_rest_min.value(),
            "rest_max_seconds":       sp.sp_rest_max.value(),
            "session_break_every":    sp.sp_switch_every.value(),
            "switch_mode":            "hours" if sp.rb_switch_hours.isChecked() else "profiles",
            "switch_hours":           sp.sp_switch_hours.value(),
        }

        # IP rotation
        cfg["ip_rotation"] = {
            "enabled":              sp.chk_ip_enabled.isChecked(),
            "interval_min_minutes": sp.sp_ip_min.value(),
            "interval_max_minutes": sp.sp_ip_max.value(),
        }

        # Main Account
        mp = self.main_account_page
        spintax_lines = [
            line.strip() for line in mp.txt_spintax.toPlainText().splitlines()
            if line.strip()
        ]
        cfg["main_account"] = {
            "enabled":    mp.chk_ma_enabled.isChecked(),
            "phone_slot": mp.combo_ma_slot.currentData() or 0,
            "working_hours_windows": mp.get_windows(),
            "daily_limit": {
                "enabled":      mp.chk_dl_enabled.isChecked(),
                "hours":        mp.sp_dl_hours.value(),
                "minutes":      mp.sp_dl_minutes.value(),
            },
            "stories": {
                "enabled":            mp.chk_st_enabled.isChecked(),
                "watch_min":          mp.sp_st_watch_min.value(),
                "watch_max":          mp.sp_st_watch_max.value(),
                "rest_short_min":     mp.sp_st_rest_s_min.value(),
                "rest_short_max":     mp.sp_st_rest_s_max.value(),
                "short_cycles_min":   mp.sp_st_cycles_min.value(),
                "short_cycles_max":   mp.sp_st_cycles_max.value(),
                "rest_long_min":      mp.sp_st_rest_l_min.value(),
                "rest_long_max":      mp.sp_st_rest_l_max.value(),
                "watch_seconds_min":  mp.sp_st_wsec_min.value(),
                "watch_seconds_max":  mp.sp_st_wsec_max.value(),
                "like_enabled":       mp.chk_st_like.isChecked(),
                "like_pct":           mp.sp_st_like_pct.value(),
                "react_enabled":      mp.chk_st_react.isChecked(),
                "react_pct":          mp.sp_st_react_pct.value(),
                "comment_enabled":    mp.chk_st_comment.isChecked(),
                "comment_pct":        mp.sp_st_comment_pct.value(),
            },
            "feed": {
                "enabled":            mp.chk_fd_enabled.isChecked(),
                "num_scrolls":        mp.sp_fd_num_scrolls.value(),
                "scroll_min":         mp.sp_fd_scroll_min.value(),
                "scroll_max":         mp.sp_fd_scroll_max.value(),
                "like_enabled":       mp.chk_fd_like.isChecked(),
                "like_pct":           mp.sp_fd_like_pct.value(),
                "comment_enabled":    mp.chk_fd_comment.isChecked(),
                "comment_pct":        mp.sp_fd_comment_pct.value(),
            },
            "reels": {
                "enabled":            mp.chk_rl_enabled.isChecked(),
                "num_reels":          mp.sp_rl_num_reels.value(),
                "watch_seconds_min":  mp.sp_rl_wsec_min.value(),
                "watch_seconds_max":  mp.sp_rl_wsec_max.value(),
                "like_enabled":       mp.chk_rl_like.isChecked(),
                "like_pct":           mp.sp_rl_like_pct.value(),
                "comment_enabled":    mp.chk_rl_comment.isChecked(),
                "comment_pct":        mp.sp_rl_comment_pct.value(),
            },
            "replies": {
                "spintax_templates":  spintax_lines,
                "openai_api_key":     mp.inp_openai_key.text().strip(),
                "openai_context":     mp.inp_openai_context.text().strip(),
            },
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
            targets_per_phone = [[] for _ in range(MAX_PHONES)]
            for i, t in enumerate(old):
                targets_per_phone[i % MAX_PHONES].append(t)
            while len(targets_per_phone) < MAX_PHONES:
                targets_per_phone.append([])

        # Determine how many slots had saved data and expand the UI to match
        slots_needed = 1
        for i, tlist in enumerate(targets_per_phone[:MAX_PHONES]):
            if tlist:
                slots_needed = i + 1
        # Also count saved device serials
        saved_devices = c.get("devices", [])
        for i, dev in enumerate(saved_devices[:MAX_PHONES]):
            if dev.get("serial"):
                slots_needed = max(slots_needed, i + 1)
        dp.show_slots(slots_needed)

        for i, tlist in enumerate(targets_per_phone[:MAX_PHONES]):
            dp.target_rows[i].setPlainText("\n".join(tlist))

        # Restore nicknames
        saved_nicks = c.get("phone_nicknames", [])
        for i, nick in enumerate(saved_nicks[:MAX_PHONES]):
            if i < len(dp.nick_edits):
                dp.nick_edits[i].setText(nick)

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
        fp.chk_skip_no_contact.setChecked(f.get("skip_no_contact", False))
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
        sp.chk_enable_sheets.setChecked(c.get("sheets_enabled", True))
        sp.inp_appium_host.setText(c.get("appium", {}).get("host", "127.0.0.1"))

        d = c.get("delays", {})
        sp.sp_prof_min.setValue(d.get("between_profiles_min", 2.0))
        sp.sp_prof_max.setValue(d.get("between_profiles_max", 5.0))
        sp.sp_scrl_min.setValue(d.get("between_scrolls_min",  1.0))
        sp.sp_scrl_max.setValue(d.get("between_scrolls_max",  3.0))
        sp.sp_rest_min.setValue(int(d.get("rest_min_seconds", 1800)))
        sp.sp_rest_max.setValue(int(d.get("rest_max_seconds", 3600)))
        sp.sp_switch_every.setValue(int(d.get("session_break_every", 50)))
        switch_mode = d.get("switch_mode", "profiles")
        sp.rb_switch_hours.setChecked(switch_mode == "hours")
        sp.rb_switch_profiles.setChecked(switch_mode != "hours")
        sp.sp_switch_hours.setValue(int(d.get("switch_hours", 1)))
        # Re-apply enabled state after loading
        sp.sp_switch_every.setEnabled(switch_mode != "hours")
        sp.sp_switch_hours.setEnabled(switch_mode == "hours")

        # IP rotation
        ip = c.get("ip_rotation", {})
        sp.chk_ip_enabled.setChecked(ip.get("enabled", False))
        sp.sp_ip_min.setValue(float(ip.get("interval_min_minutes", 5.0)))
        sp.sp_ip_max.setValue(float(ip.get("interval_max_minutes", 15.0)))
        sp.sp_ip_min.setEnabled(ip.get("enabled", False))
        sp.sp_ip_max.setEnabled(ip.get("enabled", False))

        # Main Account
        ma = c.get("main_account", {})
        mp = self.main_account_page
        mp.chk_ma_enabled.setChecked(ma.get("enabled", False))
        slot = int(ma.get("phone_slot", 0))
        idx = mp.combo_ma_slot.findData(slot)
        if idx >= 0:
            mp.combo_ma_slot.setCurrentIndex(idx)
        mp.load_windows(ma.get("working_hours_windows", []))

        dl = ma.get("daily_limit", {})
        mp.chk_dl_enabled.setChecked(dl.get("enabled", False))
        mp.sp_dl_hours.setValue(int(dl.get("hours", 4)))
        mp.sp_dl_minutes.setValue(int(dl.get("minutes", 0)))
        # Re-apply enabled/disabled state after loading values
        mp.sp_dl_hours.setEnabled(dl.get("enabled", False))
        mp.sp_dl_minutes.setEnabled(dl.get("enabled", False))

        st = ma.get("stories", {})
        mp.chk_st_enabled.setChecked(st.get("enabled", True))
        mp.sp_st_watch_min.setValue(int(st.get("watch_min", 3)))
        mp.sp_st_watch_max.setValue(int(st.get("watch_max", 7)))
        mp.sp_st_wsec_min.setValue(float(st.get("watch_seconds_min", 3.0)))
        mp.sp_st_wsec_max.setValue(float(st.get("watch_seconds_max", 8.0)))
        mp.sp_st_rest_s_min.setValue(float(st.get("rest_short_min", 10.0)))
        mp.sp_st_rest_s_max.setValue(float(st.get("rest_short_max", 30.0)))
        mp.sp_st_cycles_min.setValue(int(st.get("short_cycles_min", 3)))
        mp.sp_st_cycles_max.setValue(int(st.get("short_cycles_max", 6)))
        mp.sp_st_rest_l_min.setValue(float(st.get("rest_long_min", 120.0)))
        mp.sp_st_rest_l_max.setValue(float(st.get("rest_long_max", 300.0)))
        mp.chk_st_like.setChecked(st.get("like_enabled", True))
        mp.sp_st_like_pct.setValue(int(st.get("like_pct", 30)))
        mp.chk_st_react.setChecked(st.get("react_enabled", True))
        mp.sp_st_react_pct.setValue(int(st.get("react_pct", 20)))
        mp.chk_st_comment.setChecked(st.get("comment_enabled", False))
        mp.sp_st_comment_pct.setValue(int(st.get("comment_pct", 10)))

        fd = ma.get("feed", {})
        mp.chk_fd_enabled.setChecked(fd.get("enabled", False))
        mp.sp_fd_scroll_min.setValue(float(fd.get("scroll_min", 1.5)))
        mp.sp_fd_scroll_max.setValue(float(fd.get("scroll_max", 4.0)))
        mp.sp_fd_num_scrolls.setValue(int(fd.get("num_scrolls", 10)))
        mp.chk_fd_like.setChecked(fd.get("like_enabled", True))
        mp.sp_fd_like_pct.setValue(int(fd.get("like_pct", 40)))
        mp.chk_fd_comment.setChecked(fd.get("comment_enabled", False))
        mp.sp_fd_comment_pct.setValue(int(fd.get("comment_pct", 5)))

        rl = ma.get("reels", {})
        mp.chk_rl_enabled.setChecked(rl.get("enabled", False))
        mp.sp_rl_num_reels.setValue(int(rl.get("num_reels", 10)))
        mp.sp_rl_wsec_min.setValue(float(rl.get("watch_seconds_min", 5.0)))
        mp.sp_rl_wsec_max.setValue(float(rl.get("watch_seconds_max", 15.0)))
        mp.chk_rl_like.setChecked(rl.get("like_enabled", True))
        mp.sp_rl_like_pct.setValue(int(rl.get("like_pct", 30)))
        mp.chk_rl_comment.setChecked(rl.get("comment_enabled", False))
        mp.sp_rl_comment_pct.setValue(int(rl.get("comment_pct", 5)))

        rp = ma.get("replies", {})
        mp.txt_spintax.setPlainText("\n".join(rp.get("spintax_templates", [])))
        mp.inp_openai_key.setText(rp.get("openai_api_key", ""))
        mp.inp_openai_context.setText(rp.get("openai_context", ""))

    # ── Core scraping ─────────────────────────────────────────────────────
    def _start_scraping(self):
        cfg      = self._collect_cfg()
        assigned = self._get_assigned_devices()

        if not assigned:
            InfoBar.warning("No Devices", "Assign at least one phone in the Dashboard.", isClosable=True, duration=6000, parent=self)
            return

        phone_targets = self._get_phone_targets()
        total_targets = sum(len(t) for t in phone_targets)
        if total_targets == 0:
            InfoBar.warning(
                "No Targets",
                "Enter at least one username in any phone's target box.",
                isClosable=True,
                duration=6000,
                parent=self,
            )
            return

        save_config(cfg)

        # Capture everything _launch_scraping needs (closure over local vars)
        _cfg           = cfg
        _assigned      = assigned
        _phone_targets = phone_targets
        _total_targets = total_targets

        # ── If Google Sheets is disabled, skip auth entirely ──────────────────
        if not cfg.get("sheets_enabled", True):
            self._sheets_client = None
            self._log("ℹ️ Google Sheets disabled — skipping auth.")
            self.dashboard_page.btn_start.setEnabled(False)
            self._launch_scraping(_cfg, _assigned, _phone_targets, _total_targets)
            return

        self._log("🔗 Connecting to Google Sheets…")
        # Disable Start button while OAuth may open a browser — re-enabled on failure
        self.dashboard_page.btn_start.setEnabled(False)

        _reuse = (
            self._sheets_client is not None
            and getattr(self._sheets_client, "_worksheet", None) is not None
            and getattr(self._sheets_client, "sheet_id", None) == cfg["sheet_id"]
        )

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
                duration=8000,
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
            InfoBar.error("Appium Failed", str(e)[:300], isClosable=True, duration=8000, parent=self)
            self._log(f"❌ Appium startup failed: {e}")
            self._update_start_button_state()
            return

        self._collected     = 0
        self._active_phones = len(assigned)
        self._done_phones   = 0
        self._workers       = []
        # Store metadata needed to restart a single phone after error
        self._phone_meta: dict = {}   # row_idx -> {serial, port, targets, cfg}
        self._serial_to_port  = serial_to_port

        # Start session summary tracking
        phone_labels = {row_idx: self._phone_label(row_idx) for row_idx, _ in assigned}
        start_session(phone_labels)
        self._phone_session_counts: dict = {row_idx: 0 for row_idx, _ in assigned}

        rp = self.results_page
        rp.table.setRowCount(0)
        rp.progress_bar.setMaximum(max(cfg["last_count"] * total_targets, 1))
        rp.progress_bar.setValue(0)
        rp.lbl_progress.setText("Starting…")
        rp.log_area.clear()
        rp.ma_log_area.clear()

        # Show status labels only for slots that have a device assigned
        for i, lbl in enumerate(rp.phone_status_labels):
            has_device = (
                i < len(self.dashboard_page.device_rows) and
                self.dashboard_page.device_rows[i][0].currentData() is not None
            )
            lbl.setVisible(has_device)
            if has_device:
                lbl.setText(f"{self._phone_label(i)}: idle")

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

            # Save metadata so we can restart this phone independently
            self._phone_meta[row_idx] = {
                "serial":           serial,
                "port":             port,
                "targets":          targets_for_phone,
                "cfg":              cfg,
                "restart_attempts": 0,   # counts consecutive auto-restarts
            }

            worker = PhoneWorker(
                phone_index=row_idx,
                serial=serial,
                appium_port=port,
                targets=targets_for_phone,
                config=cfg,
                sheets_client=self._sheets_client,
            )
            worker.signals.log.connect(
                (lambda idx: lambda msg: self._log(
                    __import__("re").sub(r"^\[Phone \d+\]", f"[{self._phone_label(idx)}]", msg)
                ))(row_idx)
            )
            worker.signals.account.connect(self._on_account)
            worker.signals.progress.connect(self._on_progress)
            worker.signals.finished.connect(self._on_phone_finished)
            worker.signals.error.connect(self._on_phone_error)
            worker.signals.status.connect(self._on_phone_status)
            worker.signals.account_switched.connect(self._on_auto_switch)
            worker.signals.target_done.connect(self._on_target_done)
            worker.signals.scraped_count.connect(self._on_phone_scraped_count)
            self._workers.append(worker)

            # Enable individual stop button for this phone
            dp = self.dashboard_page
            if row_idx < len(dp.stop_phone_btns):
                btn = dp.stop_phone_btns[row_idx]
                btn.setEnabled(True)
                # Disconnect previous connections to avoid stacking
                try:
                    btn.clicked.disconnect()
                except Exception:
                    pass
                btn.clicked.connect(lambda _, idx=row_idx: self._stop_single_phone(idx))

            # Stagger worker starts by 5 s each so Appium bootstrap + account
            # detection never all hit the ADB daemon at the same instant.
            # worker_number is 0-based position in the workers list.
            worker_number = len(self._workers) - 1
            delay_ms = worker_number * 5000
            if delay_ms == 0:
                worker.start()
            else:
                QTimer.singleShot(delay_ms, worker.start)

            self._log(
                f"📱 [{self._phone_label(row_idx)}] [{serial}] started on port {port} "
                f"with {len(targets_for_phone)} target(s)"
            )

        if not self._workers:
            self._reset_ui_after_done()

    # ── Main Account ──────────────────────────────────────────────────────
    def _start_main_account(self):
        """Start the MainAccountWorker on the designated phone slot."""
        if self._ma_worker and self._ma_worker.isRunning():
            InfoBar.warning("Already Running", "Main Account is already active.", isClosable=True, duration=4000, parent=self)
            return

        cfg = self._collect_cfg()
        save_config(cfg)
        ma = cfg.get("main_account", {})

        if not ma.get("enabled", False):
            InfoBar.warning("Not Enabled", "Enable Main Account mode first.", isClosable=True, duration=5000, parent=self)
            return

        # Guard: if any comment action is enabled, require at least one template or OpenAI key
        replies  = ma.get("replies", {})
        has_text = bool(replies.get("spintax_templates")) or bool(replies.get("openai_api_key", "").strip())
        comment_anywhere = (
            ma.get("stories", {}).get("comment_enabled", False) or
            ma.get("feed",    {}).get("comment_enabled", False) or
            ma.get("reels",   {}).get("comment_enabled", False)
        )
        if comment_anywhere and not has_text:
            InfoBar.warning(
                "No Comment Templates",
                "Commenting is enabled but no spintax templates or OpenAI key configured. "
                "Add templates or disable commenting.",
                isClosable=True, duration=7000, parent=self,
            )
            return

        slot = int(ma.get("phone_slot", 0))
        dp   = self.dashboard_page

        # combo_ma_slot is kept up-to-date by _sync_ma_slot_combo / _on_nick_changed;
        # just read the already-correct labels directly.
        mp = self.main_account_page

        if slot >= len(dp.device_rows):
            InfoBar.warning("No Device", f"Phone slot {slot+1} is out of range.", isClosable=True, duration=5000, parent=self)
            return

        serial = dp.device_rows[slot][0].currentData()
        if not serial:
            label = self._phone_label(slot)
            InfoBar.warning("No Device", f"{label} has no device assigned.", isClosable=True, duration=5000, parent=self)
            return

        # Use the already-assigned port if scraping is running for this serial,
        # otherwise fall back to slot-based assignment. This prevents two
        # Appium servers from being started on different ports for the same device.
        try:
            if hasattr(self, "_serial_to_port") and serial in self._serial_to_port:
                port = self._serial_to_port[serial]
                self._appium_mgr._ensure_server(port, log_callback=self._log)
            else:
                port = self._appium_mgr.port_for_index(slot)
                self._appium_mgr._ensure_server(port, log_callback=self._log)
        except RuntimeError as e:
            InfoBar.error("Appium Failed", str(e)[:300], isClosable=True, duration=8000, parent=self)
            return

        mp = self.main_account_page
        mp.btn_ma_start.setEnabled(False)
        mp.btn_ma_stop.setEnabled(True)
        mp.lbl_ma_status.setText("Starting…")

        from src.automation.main_account_worker import MainAccountWorker
        self._ma_worker = MainAccountWorker(
            phone_index=slot,
            serial=serial,
            appium_port=port,
            config=cfg,
        )
        _ma_label = self._phone_label(slot)
        self._ma_worker.signals.log.connect(
            lambda msg, lbl=_ma_label: self._log_ma(
                __import__("re").sub(
                    r"^\[Main Acct / Phone \d+\]",
                    f"[Main Acct / {lbl}]",
                    msg
                )
            )
        )
        self._ma_worker.signals.status.connect(
            lambda s: mp.lbl_ma_status.setText(s)
        )
        self._ma_worker.signals.finished.connect(self._on_ma_finished)
        self._ma_worker.signals.error.connect(self._on_ma_error)
        self._ma_worker.start()

        # ── Clear MA log on every fresh start (same behaviour as scraping log) ──
        self.results_page.ma_log_area.clear()

        self._log_ma(f"🌟 Main Account started on {self._phone_label(slot)} [{serial}]")

        # ── Daily limit startup notice ────────────────────────────────────────
        dl = ma.get("daily_limit", {})
        if dl.get("enabled", False):
            h   = int(dl.get("hours",   4))
            m   = int(dl.get("minutes", 0))
            parts = []
            if h: parts.append(f"{h}h")
            if m: parts.append(f"{m:02d}m")
            if not parts: parts = ["0m"]
            self._log_ma(f"⏱ Daily time limit set to {' '.join(parts)} of active engagement. Resets at midnight.")
        else:
            self._log_ma("⏱ Daily time limit: disabled (running unlimited).")

        # Navigate to results page so user can see the live log
        self.stackedWidget.setCurrentWidget(self.results_page)

    def _stop_main_account(self):
        if self._ma_worker and self._ma_worker.isRunning():
            self._ma_worker.stop()
            self._log("⏹️ Main Account stop requested…")
        mp = self.main_account_page
        mp.btn_ma_stop.setEnabled(False)

    def _on_ma_finished(self):
        mp = self.main_account_page
        mp.btn_ma_start.setEnabled(True)
        mp.btn_ma_stop.setEnabled(False)
        mp.lbl_ma_status.setText("Finished")
        self._log_ma("✅ Main Account worker finished.")

    def _on_ma_error(self, msg: str):
        mp = self.main_account_page
        mp.btn_ma_start.setEnabled(True)
        mp.btn_ma_stop.setEnabled(False)
        mp.lbl_ma_status.setText("Error ❌")
        self._log_ma(f"❌ Main Account error: {msg[:200]}")
        InfoBar.error("Main Account Error", msg[:200], isClosable=True, duration=8000, parent=self)

    def _stop_all(self):
        """Stop scraping workers only. Main Account has its own Stop button."""
        for w in self._workers:
            w.stop()
        self._log("⏹️ Stop requested for all scraping phones…")
        self.dashboard_page.btn_stop.setEnabled(False)
        # Disable all individual stop buttons
        for btn in self.dashboard_page.stop_phone_btns:
            btn.setEnabled(False)

    def _stop_single_phone(self, phone_idx: int):
        """Stop a single phone worker without affecting the others."""
        for w in self._workers:
            if w.phone_index == phone_idx:
                w.stop()
                self._log(f"⏹️ Stop requested for Phone {phone_idx + 1}…")
                self._on_phone_status(phone_idx, "stopping…")
                dp = self.dashboard_page
                if phone_idx < len(dp.stop_phone_btns):
                    dp.stop_phone_btns[phone_idx].setEnabled(False)
                break

    def _on_target_done(self, phone_idx: int, username: str):
        """Called when a phone finishes scraping a target completely."""
        dp    = self.dashboard_page
        label = self._phone_label(phone_idx)
        # Mark in summary file
        mark_target_completed(username, label)
        # Remove the target from the phone's text area so the client sees what's left
        if phone_idx < len(dp.target_rows):
            txt      = dp.target_rows[phone_idx]
            existing = [t.strip() for t in txt.toPlainText().splitlines() if t.strip()]
            updated  = [t for t in existing if t.lstrip("@").lower() != username.lower()]
            txt.setPlainText("\n".join(updated))
        self._log(f"✅ [{label}] completed @{username} — logged to scraping_summary.txt")

    def _on_phone_scraped_count(self, phone_idx: int, session_count: int):
        """Update the per-phone session count label and summary tracker."""
        label = self._phone_label(phone_idx)
        record_scraped(label, session_count)
        if not hasattr(self, "_phone_session_counts"):
            self._phone_session_counts = {}
        self._phone_session_counts[phone_idx] = session_count
        rp = self.results_page
        if phone_idx < len(rp.phone_status_labels):
            rp.phone_status_labels[phone_idx].setText(f"{label}: {session_count} scraped")
        # Refresh summary card on dashboard
        dp    = self.dashboard_page
        parts = [f"{self._phone_label(i)}: {cnt}" for i, cnt in self._phone_session_counts.items()]
        dp.lbl_summary_info.setText("This session — " + " | ".join(parts))

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
            nick = ""
            if phone_idx < len(dp.nick_edits):
                nick = dp.nick_edits[phone_idx].text().strip()
            label = nick if nick else f"Phone {phone_idx + 1}"
            rp.phone_status_labels[phone_idx].setText(f"{label}: {status}")

    def _on_phone_finished(self, phone_idx: int, count: int):
        # Reset restart counter — this phone recovered and ran to completion
        meta = getattr(self, "_phone_meta", {}).get(phone_idx)
        if meta:
            meta["restart_attempts"] = 0
        self._done_phones += 1
        self._log(
            f"✅ Phone {phone_idx+1} finished — {count} accounts. "
            f"({self._done_phones}/{self._active_phones} phones done)"
        )
        self._on_phone_status(phone_idx, "done ✅")
        if self._done_phones >= self._active_phones:
            self._all_done()

    def _on_phone_error(self, phone_idx: int, msg: str):
        self._log(f"❌ Phone {phone_idx+1} error: {msg}")
        self._on_phone_status(phone_idx, "error ❌")
        InfoBar.error(f"Phone {phone_idx+1} Error", msg[:200], isClosable=True, duration=8000, parent=self)

        # ── Auto-restart this phone after a short delay ───────────────────────
        # Only restart if we still have metadata for this phone AND scraping
        # has not been fully stopped by the user (btn_stop still enabled means
        # at least one phone was running).
        meta = getattr(self, "_phone_meta", {}).get(phone_idx)
        stop_btn_active = self.dashboard_page.btn_stop.isEnabled()

        _MAX_RESTARTS = 3

        if meta and stop_btn_active:
            meta["restart_attempts"] += 1
            if meta["restart_attempts"] > _MAX_RESTARTS:
                self._log(
                    f"🛑 Phone {phone_idx+1} failed {_MAX_RESTARTS} restart attempts — giving up."
                )
                self._on_phone_status(phone_idx, "failed ❌")
                self._done_phones += 1
                if self._done_phones >= self._active_phones:
                    self._all_done()
                return
            self._log(
                f"🔄 Phone {phone_idx+1} will auto-restart in 15 s… "
                f"(attempt {meta['restart_attempts']}/{_MAX_RESTARTS})"
            )
            self._on_phone_status(phone_idx, "restarting…")
            QTimer.singleShot(15_000, lambda: self._restart_phone(phone_idx))
        else:
            # No restart possible — count this phone as done
            self._done_phones += 1
            if self._done_phones >= self._active_phones:
                self._all_done()

    def _wait_for_device(self, serial: str, timeout: int = 120) -> bool:
        """
        Block until the device is fully booted and ADB-reachable.
        Returns True if ready within timeout seconds, False otherwise.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                result = subprocess.run(
                    ["adb", "-s", serial, "get-state"],
                    capture_output=True, text=True, timeout=5
                )
                if result.stdout.strip() == "device":
                    boot = subprocess.run(
                        ["adb", "-s", serial, "shell",
                         "getprop", "sys.boot_completed"],
                        capture_output=True, text=True, timeout=5
                    )
                    if boot.stdout.strip() == "1":
                        return True
            except Exception:
                pass
            time.sleep(3)
        return False

    def _restart_phone(self, phone_idx: int):
        """Spawn a fresh PhoneWorker for phone_idx using saved metadata."""
        meta = getattr(self, "_phone_meta", {}).get(phone_idx)
        if not meta:
            return
        # If user pressed Stop while the 15 s timer was running, abort.
        if not self.dashboard_page.btn_stop.isEnabled():
            self._done_phones += 1
            if self._done_phones >= self._active_phones:
                self._all_done()
            return

        serial  = meta["serial"]
        port    = meta["port"]
        targets = meta["targets"]
        cfg     = meta["cfg"]

        self._log(f"🔄 Restarting Phone {phone_idx+1} [{serial}]…")
        self._on_phone_status(phone_idx, "Waiting for device…")

        # ── Wait for device to be fully booted before spawning worker ─────────
        # Without this, get_instagram_accounts() runs while the device is still
        # booting and returns the fake ['Account 1'] fallback, causing the new
        # session to fail immediately.
        # Run in a background thread so we never block the Qt main thread.
        def _wait_and_spawn():
            self._log(f"⏳ Phone {phone_idx+1} — waiting for device to be ready (up to 120 s)…")
            if not self._wait_for_device(serial, timeout=120):
                self._log(f"⚠️ Phone {phone_idx+1} — device not reachable after 120 s, will retry…")
                self._on_phone_error(phone_idx, f"Device {serial} did not come back online in time")
                return

            # Extra grace period so Instagram finishes loading after boot
            self._log(f"✅ Phone {phone_idx+1} — device ready. Waiting 10 s for apps to settle…")
            time.sleep(10)
            self._on_phone_status(phone_idx, "Restarting…")

            worker = PhoneWorker(
                phone_index=phone_idx,
                serial=serial,
                appium_port=port,
                targets=targets,
                config=cfg,
                sheets_client=self._sheets_client,
            )
            worker.signals.log.connect(
                (lambda idx: lambda msg: self._log(
                    __import__("re").sub(r"^\[Phone \d+\]", f"[{self._phone_label(idx)}]", msg)
                ))(phone_idx)
            )
            worker.signals.account.connect(self._on_account)
            worker.signals.progress.connect(self._on_progress)
            worker.signals.finished.connect(self._on_phone_finished)
            worker.signals.error.connect(self._on_phone_error)
            worker.signals.status.connect(self._on_phone_status)
            worker.signals.account_switched.connect(self._on_auto_switch)
            worker.signals.target_done.connect(self._on_target_done)
            worker.signals.scraped_count.connect(self._on_phone_scraped_count)

            # Replace the old (dead) worker entry in self._workers
            for i, w in enumerate(self._workers):
                if w.phone_index == phone_idx:
                    self._workers[i] = worker
                    break
            else:
                self._workers.append(worker)

            worker.start()
            self._log(f"📱 Phone {phone_idx+1} restarted on port {port}.")

        threading.Thread(target=_wait_and_spawn, daemon=True).start()

    def _all_done(self):
        self._log(f"🏁 All phones done. Total collected: {self._collected} accounts.")
        finish_session(self._collected)
        self._reload_blacklist_ui()
        # Update summary card
        dp = self.dashboard_page
        dp.lbl_summary_info.setText(
            f"Last session: {self._collected} total accounts collected. "
            f"Summary saved to scraping_summary.txt"
        )
        InfoBar.success(
            "Complete!",
            f"All phones finished. {self._collected} accounts collected.",
            isClosable=True,
            duration=8000,
            parent=self,
        )
        self._reset_ui_after_done()

    def _reset_ui_after_done(self):
        self.dashboard_page.btn_start.setEnabled(True)
        self.dashboard_page.btn_stop.setEnabled(False)
        self._set_schedule_locked(False)
        self.dashboard_page.lbl_overall_status.setText(f"Done — {self._collected} collected")
        self.results_page.lbl_progress.setText(f"Done: {self._collected} total accounts")
        # Disable all individual stop buttons
        for btn in self.dashboard_page.stop_phone_btns:
            btn.setEnabled(False)

    # ── Logging ───────────────────────────────────────────────────────────
    def _phone_label(self, phone_idx: int) -> str:
        """Return nickname if set, else 'Phone N'."""
        dp = self.dashboard_page
        if phone_idx < len(dp.nick_edits):
            nick = dp.nick_edits[phone_idx].text().strip()
            if nick:
                return nick
        return f"Phone {phone_idx + 1}"

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.results_page.log_area.append(f"[{ts}] {msg}")

    def _log_ma(self, msg: str):
        """Route Main Account messages to the dedicated MA log panel."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.results_page.ma_log_area.append(f"[{ts}] {msg}")

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
            InfoBar.warning("Missing Sheet ID", "Enter your Google Sheet ID first.", isClosable=True, duration=6000, parent=self)
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
            InfoBar.success("Connected!", f"Google Sheets ready. {rows} existing rows.", isClosable=True, duration=5000, parent=self)
            try:
                self.settings_page.btn_test_sheets.setEnabled(True)
            except Exception:
                pass

        def _on_test_failure(err_msg):
            self._sheets_client = None
            self.settings_page.lbl_sheet_status.setText("❌ Not connected")
            self._log(f"❌ Sheets error: {err_msg}")
            InfoBar.error("Connection Failed", err_msg, isClosable=True, duration=8000, parent=self)
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
            InfoBar.success("Token Revoked", "Will re-authenticate on next connect.", isClosable=True, duration=5000, parent=self)
        else:
            InfoBar.info("No Token", "No saved token found.", isClosable=True, duration=5000, parent=self)

    # ── Blacklist helpers ─────────────────────────────────────────────────
    def _reload_blacklist_ui(self):
        """Refresh the count labels from the on-disk blacklists."""
        bl = load_blacklist()
        count = len(bl)
        self.filters_page.lbl_bl_count.setText(
            f"{count} entr{'y' if count == 1 else 'ies'} in blacklist"
        )
        kw_bl = load_keyword_blacklist()
        kw_count = len(kw_bl)
        self.filters_page.lbl_kw_bl_count.setText(
            f"{kw_count} entr{'y' if kw_count == 1 else 'ies'} in keyword blacklist"
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
                isClosable=True,
                duration=5000,
                parent=self,
            )
        except Exception as exc:
            InfoBar.error("Export Failed", str(exc), isClosable=True, duration=8000, parent=self)

    def _download_summary(self):
        """Let the user save a copy of scraping_summary.txt to any location they choose."""
        src = get_summary_path()
        if not os.path.exists(src):
            InfoBar.warning(
                "No Summary Yet",
                "No sessions have been run yet. Run a scraping session first.",
                isClosable=True, duration=5000, parent=self,
            )
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save Session Summary", "scraping_summary.txt", "Text files (*.txt)"
        )
        if not dest:
            return
        try:
            import shutil
            shutil.copy2(src, dest)
            InfoBar.success(
                "Summary Saved",
                f"Session summary exported → {os.path.basename(dest)}",
                isClosable=True, duration=5000, parent=self,
            )
        except Exception as exc:
            InfoBar.error("Export Failed", str(exc), isClosable=True, duration=8000, parent=self)

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
                isClosable=True,
                duration=5000,
                parent=self,
            )
        except Exception as exc:
            InfoBar.error("Import Failed", str(exc), isClosable=True, duration=8000, parent=self)

    def _clear_blacklist(self):
        if QMessageBox.question(
            self, "Clear Blacklist",
            "Clear the entire blacklist?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            clear_blacklist()
            self._reload_blacklist_ui()
            InfoBar.success("Cleared", "Blacklist cleared.", isClosable=True, duration=5000, parent=self)

    # ── Keyword blacklist handlers ─────────────────────────────────────────

    def _download_kw_blacklist_txt(self):
        """Export the keyword blacklist as a plain .txt file."""
        bl = load_keyword_blacklist()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Keyword Blacklist", "blacklist_keyword.txt", "Text files (*.txt)"
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
                f"Keyword blacklist exported ({len(bl)} entries) → {os.path.basename(path)}",
                isClosable=True, duration=5000, parent=self,
            )
        except Exception as exc:
            InfoBar.error("Export Failed", str(exc), isClosable=True, duration=8000, parent=self)

    def _import_kw_blacklist_txt(self):
        """Import usernames from a .txt file into the keyword blacklist (merges)."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Keyword Blacklist", "", "Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                new_entries = {line.strip().lower() for line in f if line.strip()}
            bl = load_keyword_blacklist()
            before = len(bl)
            bl.update(new_entries)
            save_keyword_blacklist(bl)
            added = len(bl) - before
            self._reload_blacklist_ui()
            InfoBar.success(
                "Imported",
                f"Added {added} new entr{'y' if added == 1 else 'ies'} "
                f"({len(bl)} total in keyword blacklist).",
                isClosable=True, duration=5000, parent=self,
            )
        except Exception as exc:
            InfoBar.error("Import Failed", str(exc), isClosable=True, duration=8000, parent=self)

    def _clear_kw_blacklist(self):
        if QMessageBox.question(
            self, "Clear Keyword Blacklist",
            "Clear the entire keyword blacklist?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            clear_keyword_blacklist()
            self._reload_blacklist_ui()
            InfoBar.success("Cleared", "Keyword blacklist cleared.", isClosable=True, duration=5000, parent=self)

    # ── Export ────────────────────────────────────────────────────────────
    def _export_csv(self):
        table = self.results_page.table
        if table.rowCount() == 0:
            InfoBar.warning("No Data", "No accounts collected yet.", isClosable=True, duration=6000, parent=self)
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
            InfoBar.success("Exported", f"{table.rowCount()} rows saved to {path}", isClosable=True, duration=5000, parent=self)
        except Exception as e:
            InfoBar.error("Export Failed", str(e), isClosable=True, duration=8000, parent=self)

    # ── Cleanup ───────────────────────────────────────────────────────────
    def closeEvent(self, event):
        # ── Exit confirmation ─────────────────────────────────────────────────
        reply = QMessageBox.question(
            self,
            "Exit Cansa",
            "Are you sure you want to exit?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        cfg = self._collect_cfg()
        cfg["mirror_width"] = getattr(self, "_mirror_width", 500)
        save_config(cfg)
        for w in self._workers:
            w.stop()
        for w in self._workers:
            w.wait(3000)
        if self._ma_worker and self._ma_worker.isRunning():
            self._ma_worker.stop()
            self._ma_worker.wait(3000)
        self._appium_mgr.stop_all()
        if hasattr(self, "mirror"):
            self.mirror.detach()
        event.accept()


    # ── Network monitor ───────────────────────────────────────────────────
    def _setup_network_monitor(self):
        """
        Poll internet connectivity every 15 seconds using Qt's own network
        stack (no extra threads, no sockets).  Shows an InfoBar warning when
        the connection drops and a success bar when it comes back.
        """
        self._net_manager   = QNetworkAccessManager(self)
        self._net_was_up    = True   # assume online at start
        self._net_bar_shown = False  # track whether the warning bar is live
        self._net_fail_count = 0     # consecutive failures needed before showing toast
        self._net_bar       = None   # reference to the live "No Internet" InfoBar

        self._net_timer = QTimer(self)
        self._net_timer.setInterval(15_000)   # check every 15 s
        self._net_timer.timeout.connect(self._check_network)
        self._net_timer.start()

        # Run one check ~2 s after startup so the window is fully painted first
        QTimer.singleShot(2_000, self._check_network)

    def _check_network(self):
        """Fire a lightweight HEAD request to check connectivity."""
        req = QNetworkRequest(QUrl("https://www.google.com"))
        req.setTransferTimeout(6_000)
        reply = self._net_manager.head(req)
        reply.finished.connect(lambda: self._on_net_reply(reply))

    def _on_net_reply(self, reply):
        from PyQt6.QtNetwork import QNetworkReply
        err = reply.error()
        reply.deleteLater()

        is_up = (err == QNetworkReply.NetworkError.NoError)

        if not is_up and self._net_was_up:
            # Require 2 consecutive failures before declaring offline
            # (prevents false positives from a single slow/blocked request on startup)
            self._net_fail_count += 1
            if self._net_fail_count >= 2:
                self._net_was_up     = False
                self._net_bar_shown  = True
                self._net_fail_count = 0
                self._net_bar = InfoBar.warning(
                    "No Internet",
                    "No internet connection detected. Please check your network.",
                    isClosable=True,
                    duration=-1,      # stays until dismissed or connection returns (-1 = infinite in QFluentWidgets)
                    parent=self,
                )

        elif is_up and not self._net_was_up:
            # Connection restored — close the yellow bar immediately
            if self._net_bar is not None:
                self._net_bar.close()
                self._net_bar = None
            self._net_was_up     = True
            self._net_bar_shown  = False
            self._net_fail_count = 0
            InfoBar.success(
                "Back Online",
                "Internet connection restored.",
                isClosable=True,
                duration=8_000,
                parent=self,
            )
        elif is_up:
            # Still online — reset any partial fail streak
            self._net_fail_count = 0


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    # Use the correct ICO path relative to the project root (two levels up from src/ui/)
    _ico = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "cansa_icon.ico"))
    if os.path.exists(_ico):
        app.setWindowIcon(QIcon(_ico))
    from PyQt6.QtWidgets import QToolTip
    app.setStyleSheet(
        "QToolTip {"
        "  background-color: #1e293b;"
        "  color: #f1f5f9;"
        "  border: 1px solid #334155;"
        "  padding: 4px 8px;"
        "  border-radius: 4px;"
        "  font-size: 12px;"
        "}"
    )
    window = MainWindow()
    window.show()
    sys.exit(app.exec())