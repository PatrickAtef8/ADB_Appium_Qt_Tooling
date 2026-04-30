"""
main_account_worker.py — "Main Account" engagement worker.

Engagement model (per client spec):
  Stories
  ────────
  • Watch MIN–MAX stories, then rest MIN–MAX seconds, then resume — repeat
    this micro-cycle MIN–MAX times (short_cycles), then take a longer rest,
    then repeat the whole pattern.
  • Per story: randomly like / react / comment based on individual %
    probabilities that can each be toggled off.
  • React emoji pool: 😮 ❤️ 👏 🔥 (the four the client circled in the image)

  Feed & Reels
  ─────────────
  • Separate enable/disable toggles, same like/react/comment % system.
  • Feed: scroll and engage with posts.
  • Reels: open the Reels tab and engage.

  Working hours: multiple time windows — starts immediately if already in window.
  Replies: spintax engine + optional OpenAI API (unchanged).
  IP rotation (unchanged).
"""

from __future__ import annotations

import random
import re
import time
import traceback
from datetime import datetime, time as dtime, timedelta
from typing import Callable, List, Optional, Dict, Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal


# ── Spintax engine ─────────────────────────────────────────────────────────────

def spin(text: str) -> str:
    def _replace(m: re.Match) -> str:
        choices = m.group(1).split("|")
        return random.choice(choices)
    prev = None
    result = text
    while prev != result:
        prev = result
        result = re.sub(r"\{([^{}]+)\}", _replace, result)
    return result


# ── OpenAI reply helper ────────────────────────────────────────────────────────

def generate_openai_reply(
    api_key: str,
    context: str,
    system_prompt: str = "You are a friendly Instagram commenter. Write a short, natural reply.",
    max_tokens: int = 60,
) -> str:
    try:
        import urllib.request, json
        payload = json.dumps({
            "model": "gpt-4o-mini",
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": context},
            ],
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


# ── Working-hours helpers ──────────────────────────────────────────────────────

def _parse_time_window(w: dict):
    return (
        dtime(int(w["start_hour"]), int(w["start_minute"])),
        dtime(int(w["end_hour"]),   int(w["end_minute"])),
    )


def _in_any_window(windows: List[dict]) -> bool:
    """Return True if 'now' falls inside any configured time window."""
    now_t = datetime.now().time()
    for w in windows:
        start_t, end_t = _parse_time_window(w)
        if end_t > start_t:
            if start_t <= now_t < end_t:
                return True
        else:  # spans midnight
            if now_t >= start_t or now_t < end_t:
                return True
    return False


def _seconds_until_next_window(windows: List[dict]) -> float:
    """Return seconds until the earliest next window start.
    If we are currently INSIDE a window, skip to the next day's start for that
    window — this function is only called when we are outside all windows.
    """
    now = datetime.now()
    now_t = now.time()
    candidates = []
    for w in windows:
        start_t, end_t = _parse_time_window(w)
        in_window = (
            (end_t > start_t and start_t <= now_t < end_t) or
            (end_t <= start_t and (now_t >= start_t or now_t < end_t))
        )
        candidate = now.replace(
            hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0
        )
        if in_window or candidate <= now:
            candidate += timedelta(days=1)
        candidates.append((candidate - now).total_seconds())
    return min(candidates) if candidates else 3600.0


# ── Signals ────────────────────────────────────────────────────────────────────

class MainAccountSignals(QObject):
    log      = pyqtSignal(str)
    status   = pyqtSignal(str)
    finished = pyqtSignal()
    error    = pyqtSignal(str)


# ── Story reaction emoji pool (the four the client circled) ───────────────────
# Story reaction emojis confirmed from XML dump of reaction panel.
# Instagram's grid (3×2): row1=[😂,😮,😍]  row2=[😢,👏,🔥]
# Client-approved set: 😮 ❤️ 👏 🔥  — ❤️ is not in the grid so we use 😍 (heart-eyes)
# for the react action; the toolbar_like_button handles ❤️ likes separately.
STORY_REACTION_EMOJIS = ["😮", "😍", "👏", "🔥"]

# Exact centre coords of each emoji slot on the reference device (1440×2560).
# Derived from story_sendbar.xml dump (story_reactions_emoji bounds).
# slot order: row-major left-to-right, top-to-bottom.
#   slot0=😂(368,618)  slot1=😮(720,618)  slot2=😍(1072,618)
#   slot3=😢(368,930)  slot4=👏(720,930)  slot5=🔥(1072,930)
_EMOJI_COORDS_REF = {
    "😮": (720,  618),
    "😍": (1072, 618),
    "👏": (720,  930),
    "🔥": (1072, 930),
}


# ── Worker ─────────────────────────────────────────────────────────────────────

class MainAccountWorker(QThread):
    """
    QThread that drives a single phone as the "Main Account".

    Config keys (under cfg["main_account"]):
      working_hours_windows  – list of {start_hour, start_minute, end_hour, end_minute}
      stories                – story engagement sub-config (see below)
      feed                   – feed engagement sub-config
      reels                  – reels engagement sub-config
      replies                – spintax / OpenAI reply config

    Stories sub-config keys:
      enabled                     bool
      watch_min / watch_max       int  (stories per micro-cycle)
      rest_short_min / rest_short_max  float seconds (rest between micro-cycles)
      short_cycles_min / short_cycles_max  int (micro-cycles before long rest)
      rest_long_min / rest_long_max        float seconds (long rest)
      watch_seconds_min / watch_seconds_max  float (seconds to watch each story)
      like_enabled / like_pct     bool / float 0-100
      react_enabled / react_pct   bool / float 0-100
      comment_enabled / comment_pct  bool / float 0-100

    Feed / Reels sub-config keys (same engagement trio):
      enabled
      scroll_min / scroll_max     float seconds
      num_scrolls                 int
      like_enabled / like_pct
      react_enabled / react_pct
      comment_enabled / comment_pct
    """

    def __init__(self, phone_index: int, serial: str, appium_port: int, config: dict):
        super().__init__()
        self.phone_index = phone_index
        self.serial      = serial
        self.appium_port = appium_port
        self.config      = config
        self.signals     = MainAccountSignals()
        self._stop_flag  = False
        self._controller = None

    def stop(self):
        self._stop_flag = True

    def _log(self, msg: str):
        self.signals.log.emit(f"[Main Acct / Phone {self.phone_index + 1}] {msg}")

    def _status(self, msg: str):
        self.signals.status.emit(msg)

    def _sleep(self, seconds: float):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop_flag:
            time.sleep(min(0.5, deadline - time.monotonic()))

    # ── Main thread entry ──────────────────────────────────────────────────────

    def run(self):
        cfg = self.config
        ma  = cfg.get("main_account", {})

        ip_cfg     = cfg.get("ip_rotation", {})
        ip_enabled = ip_cfg.get("enabled", False)
        ip_min     = float(ip_cfg.get("interval_min_minutes", 5))
        ip_max     = float(ip_cfg.get("interval_max_minutes", 15))

        story_cfg  = ma.get("stories", {})
        feed_cfg   = ma.get("feed",    {})
        reels_cfg  = ma.get("reels",   {})
        reply_cfg  = ma.get("replies", {})
        windows    = ma.get("working_hours_windows", [])

        dl_cfg      = ma.get("daily_limit", {})
        dl_enabled  = dl_cfg.get("enabled", False)
        dl_seconds  = (int(dl_cfg.get("hours", 4)) * 3600
                       + int(dl_cfg.get("minutes", 0)) * 60)

        # Accumulated active engagement seconds today (resets at midnight)
        _active_today   = 0.0          # seconds counted today
        _last_date      = datetime.now().date()   # track date for midnight reset

        openai_key   = reply_cfg.get("openai_api_key", "").strip()
        spintax_pool = reply_cfg.get("spintax_templates", [])

        try:
            ip_rotator = None
            if ip_enabled:
                from src.automation.ip_rotator import IPRotator
                ip_rotator = IPRotator(
                    serial=self.serial,
                    interval_min=ip_min,
                    interval_max=ip_max,
                    log_callback=self._log,
                )
                self._log(f"🔄 IP rotation enabled: every {ip_min}–{ip_max} min")

            self._log("📱 Starting Appium session for Main Account…")
            from src.automation.appium_controller import AppiumController
            self._controller = AppiumController(
                host=cfg["appium"]["host"],
                port=self.appium_port,
            )
            self._controller.start_session(self.serial)
            self._log("✅ Appium session started.")
            driver = self._controller.driver

            while not self._stop_flag:
                # ── Working hours check ──────────────────────────────────────
                if windows:
                    if not _in_any_window(windows):
                        secs = _seconds_until_next_window(windows)
                        self._log(
                            f"⏰ Outside working hours. "
                            f"Next window in {secs/60:.1f} min. Sleeping…"
                        )
                        self._status("waiting for window")
                        self._sleep(min(secs, 60))
                        continue

                # ── Midnight reset + daily limit check ───────────────────────
                _today = datetime.now().date()
                if _today != _last_date:
                    # New day — reset counter
                    _active_today = 0.0
                    _last_date    = _today
                    self._log("🌅 New day — daily time limit counter reset.")

                if dl_enabled and dl_seconds > 0 and _active_today >= dl_seconds:
                    h, m = divmod(int(_active_today), 3600)
                    m //= 60
                    self._log(
                        f"⏹ Daily time limit reached ({h}h {m:02d}m). "
                        f"Stopping until midnight."
                    )
                    self._status("daily limit reached")
                    # Sleep in short chunks until midnight so we can respond to stop()
                    while not self._stop_flag:
                        now = datetime.now()
                        midnight = (now + timedelta(days=1)).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        secs_left = (midnight - now).total_seconds()
                        if secs_left <= 0:
                            break
                        self._sleep(min(secs_left, 60))
                        # Check if date has rolled over (reset happened)
                        if datetime.now().date() != _last_date:
                            _active_today = 0.0
                            _last_date    = datetime.now().date()
                            self._log("🌅 New day — resuming after daily limit.")
                            break
                    continue

                # ── IP rotation tick ─────────────────────────────────────────
                if ip_rotator and ip_rotator.tick():
                    self._log("🔗 Reconnecting Appium after IP rotation…")
                    try:
                        self._controller.reattach_after_adb()
                        driver = self._controller.driver
                    except Exception as e:
                        self._log(f"⚠️ Reattach after rotation failed: {e}")

                if self._stop_flag:
                    break

                # ── Stories ──────────────────────────────────────────────────
                if story_cfg.get("enabled", True):
                    self._status("watching stories")
                    _t0 = time.monotonic()
                    try:
                        self._do_stories(driver, story_cfg, reply_cfg, openai_key, spintax_pool)
                    except Exception as e:
                        if self._is_connection_error(e):
                            self._log(f"⚠️ Appium connection lost during stories: {e}")
                            if self._reconnect_appium():
                                driver = self._controller.driver
                                continue
                            else:
                                break
                        raise
                    finally:
                        _active_today += time.monotonic() - _t0

                if self._stop_flag:
                    break

                # ── Transition rest: Stories → Feed ──────────────────────
                if story_cfg.get("enabled", True) and feed_cfg.get("enabled", False):
                    _tr = random.uniform(5, 15)
                    self._log(f"⏸ Transition rest {_tr:.0f}s (stories→feed)…")
                    self._sleep(_tr)

                if self._stop_flag:
                    break

                # ── Feed ─────────────────────────────────────────────────────
                if feed_cfg.get("enabled", False):
                    self._status("browsing feed")
                    _t0 = time.monotonic()
                    try:
                        self._do_feed(driver, feed_cfg, reply_cfg, openai_key, spintax_pool)
                    except Exception as e:
                        if self._is_connection_error(e):
                            self._log(f"⚠️ Appium connection lost during feed: {e}")
                            if self._reconnect_appium():
                                driver = self._controller.driver
                                continue
                            else:
                                break
                        raise
                    finally:
                        _active_today += time.monotonic() - _t0

                if self._stop_flag:
                    break

                # ── Transition rest: Feed → Reels ─────────────────────────
                if feed_cfg.get("enabled", False) and reels_cfg.get("enabled", False):
                    _tr = random.uniform(5, 15)
                    self._log(f"⏸ Transition rest {_tr:.0f}s (feed→reels)…")
                    self._sleep(_tr)

                if self._stop_flag:
                    break

                # ── Reels ────────────────────────────────────────────────────
                if reels_cfg.get("enabled", False):
                    self._status("watching reels")
                    _t0 = time.monotonic()
                    try:
                        self._do_reels(driver, reels_cfg, reply_cfg, openai_key, spintax_pool)
                    except Exception as e:
                        if self._is_connection_error(e):
                            self._log(f"⚠️ Appium connection lost during reels: {e}")
                            if self._reconnect_appium():
                                driver = self._controller.driver
                                continue
                            else:
                                break
                        raise
                    finally:
                        _active_today += time.monotonic() - _t0

                # ── Daily limit progress log ──────────────────────────────────
                if dl_enabled and dl_seconds > 0:
                    remaining = max(0.0, dl_seconds - _active_today)
                    h_used, rem = divmod(int(_active_today), 3600)
                    m_used = rem // 60
                    h_rem,  rem = divmod(int(remaining), 3600)
                    m_rem  = rem // 60
                    self._log(
                        f"📊 Active today: {h_used}h {m_used:02d}m  |  "
                        f"Remaining: {h_rem}h {m_rem:02d}m"
                    )

                # ── Short rest before next cycle ─────────────────────────────
                cycle_rest = random.uniform(30, 90)
                self._log(f"😴 Cycle rest {cycle_rest:.0f}s…")
                self._sleep(cycle_rest)

            self.signals.finished.emit()

        except Exception as exc:
            self.signals.error.emit(
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )
        finally:
            if self._controller:
                try:
                    self._controller.stop_session()
                except Exception:
                    pass

    # ── Stories ────────────────────────────────────────────────────────────────

    def _do_stories(self, driver, story_cfg: dict, reply_cfg: dict,
                    openai_key: str, spintax_pool: list):
        """
        Micro-cycle story watching pattern:
          1. Go home once, open the first unseen story
          2. Watch MIN–MAX stories, engaging as configured
          3. Press Back → lands back on home feed with tray intact
          4. Short rest, then find next unseen story in the tray (scroll if needed)
          5. Repeat for all cycles
          6. If tray is exhausted → refresh home feed once to catch new uploads
          7. If still none → log "no active stories" and end session
          8. Long rest after all cycles complete
        """
        watch_min   = int(story_cfg.get("watch_min", 3))
        watch_max   = int(story_cfg.get("watch_max", 7))
        rest_s_min  = float(story_cfg.get("rest_short_min", 10))
        rest_s_max  = float(story_cfg.get("rest_short_max", 30))
        cycles_min  = int(story_cfg.get("short_cycles_min", 3))
        cycles_max  = int(story_cfg.get("short_cycles_max", 6))
        rest_l_min  = float(story_cfg.get("rest_long_min", 120))
        rest_l_max  = float(story_cfg.get("rest_long_max", 300))
        watch_s_min = float(story_cfg.get("watch_seconds_min", 3))
        watch_s_max = float(story_cfg.get("watch_seconds_max", 8))

        like_on    = story_cfg.get("like_enabled", True)
        like_pct   = float(story_cfg.get("like_pct", 30)) / 100.0
        react_on   = story_cfg.get("react_enabled", True)
        react_pct  = float(story_cfg.get("react_pct", 20)) / 100.0
        comment_on = story_cfg.get("comment_enabled", False)
        comment_pct= float(story_cfg.get("comment_pct", 10)) / 100.0

        num_cycles = random.randint(cycles_min, cycles_max)
        self._log(f"📖 Starting {num_cycles} story micro-cycles…")

        seen_usernames: set = set()  # kept only for logging clarity, not for blocking

        try:
            # ── Go home and pull-to-refresh the feed ──────────────────────────
            self._go_home(driver)
            self._sleep(1.5)
            # Pull-to-refresh: swipe down from top of feed to force tray reload
            try:
                size = driver.get_window_size()
                driver.swipe(
                    size["width"] // 2, int(size["height"] * 0.30),
                    size["width"] // 2, int(size["height"] * 0.75),
                    duration=600,
                )
            except Exception:
                pass
            self._sleep(2.0)

            refreshed_once = True  # already refreshed at start

            for cycle_idx in range(num_cycles):
                if self._stop_flag:
                    break

                num_stories = random.randint(watch_min, watch_max)
                self._log(f"  Cycle {cycle_idx+1}/{num_cycles}: watching {num_stories} stories…")

                if cycle_idx > 0:
                    self._scroll_story_tray(driver)
                    self._sleep(0.8)

                opened_username = self._open_next_unseen_story(driver, seen_usernames)

                if opened_username is None:
                    if not refreshed_once:
                        self._log("ℹ️ All visible stories seen — refreshing feed for new uploads…")
                        self._go_home(driver)
                        self._sleep(2.5)
                        refreshed_once = True
                        opened_username = self._open_next_unseen_story(driver, seen_usernames)

                    if opened_username is None:
                        self._log("ℹ️ No active stories available right now — ending session early.")
                        break

                seen_usernames.add(opened_username)

                drifted = False
                stories_watched = 0
                while stories_watched < num_stories:
                    if self._stop_flag:
                        break

                    watch_s       = random.uniform(watch_s_min, watch_s_max)
                    auto_advanced = False
                    self._log(f"    👁️ Story {stories_watched+1}/{num_stories} — watching {watch_s:.1f}s")

                    # ── Engage immediately on story open ────────────────────────
                    if like_on and random.random() < like_pct:
                        self._story_like(driver)
                    if react_on and random.random() < react_pct:
                        self._story_react(driver)
                    if comment_on and random.random() < comment_pct:
                        self._story_reply(driver, spintax_pool, openai_key, reply_cfg)

                    stories_watched += 1
                    # Engagement done — no post-engage polling needed.
                    # Composer dismisses automatically. Proceed to watch loop.

                    # ── Watch polling loop ──────────────────────────────────────
                    elapsed = 0.0
                    poll    = 0.5
                    while elapsed < watch_s:
                        self._sleep(poll)
                        elapsed += poll

                        if self._is_suggestions_page(driver):
                            self._log("⚠️ Suggestions page — pressing back")
                            try:
                                driver.back()
                            except Exception:
                                pass
                            # Wait up to 2s for story screen to return
                            for _ in range(4):
                                self._sleep(0.5)
                                if self._is_on_story_screen(driver):
                                    break
                            else:
                                auto_advanced = True
                            break

                        if not self._is_on_story_screen(driver):
                            # Instagram auto-advanced to next story
                            auto_advanced = True
                            break

                    if auto_advanced:
                        # Instagram moved on by itself — the next story is already
                        # showing. Don't force-advance; loop continues naturally
                        # and will engage+watch the new current story.
                        continue

                    # Force-advance to next story
                    if self._is_on_story_screen(driver):
                        self._advance_story(driver)
                        self._sleep(random.uniform(0.4, 0.9))

                    # Check if we're still on a story after advancing
                    if not self._is_on_story_screen(driver):
                        # End of this person's stories or drifted
                        break

                # Return to home feed correctly depending on exit reason
                if drifted:
                    self._recover_to_home(driver)
                    self._sleep(1.5)
                    refreshed_once = False
                else:
                    # Normal exit — back() from story viewer lands on home feed cleanly
                    try:
                        driver.back()
                    except Exception:
                        pass
                    self._sleep(1.5)

                if cycle_idx < num_cycles - 1:
                    rest = random.uniform(rest_s_min, rest_s_max)
                    self._log(f"  😴 Short rest {rest:.0f}s before next cycle…")
                    self._sleep(rest)

            rest_long = random.uniform(rest_l_min, rest_l_max)
            self._log(f"😴 Long rest {rest_long:.0f}s after story session…")
            self._sleep(rest_long)
            self._log("✅ Stories session complete.")

        except Exception as exc:
            self._log(f"⚠️ Stories error: {exc}")
            try:
                driver.back()
            except Exception:
                pass

    def _scroll_story_tray(self, driver):
        """Swipe the story tray left to reveal the next batch of unseen rings."""
        try:
            size = driver.get_window_size()
            w, h = size["width"], size["height"]
            # Story tray centre Y ≈ 16% from top (from XML: bounds [320,729] → cy≈524)
            tray_y = int(h * 0.20)
            # Swipe from 80% right to 20% left
            driver.swipe(
                int(w * 0.80), tray_y,
                int(w * 0.20), tray_y,
                duration=400,
            )
        except Exception:
            pass

    def _is_on_story_screen(self, driver) -> bool:
        """
        Verify we are currently watching a story (not drifted to feed/reels/DMs).
        Checks for elements that only exist on the story viewer screen:
          - message_composer_container (Send message bar) — confirmed from XML
          - toolbar_like_button — confirmed from XML
          - story progress bar (reel_viewer_progress)
        Returns True only if at least one story-screen element is found.
        Uses a single XPATH OR query = one Appium round-trip (~300ms) instead
        of three sequential find_elements calls (~900-1500ms).
        """
        from appium.webdriver.common.appiumby import AppiumBy
        try:
            els = driver.find_elements(
                AppiumBy.XPATH,
                '//*[@resource-id="com.instagram.android:id/message_composer_container" or '
                '@resource-id="com.instagram.android:id/toolbar_like_button" or '
                '@resource-id="com.instagram.android:id/reel_viewer_progress"]',
            )
            return bool(els)
        except Exception:
            return False

    def _is_suggestions_page(self, driver) -> bool:
        """
        Detect the 'Suggested accounts' interstitial that appears mid-story tray.
        ⚠️ STUB — replace IDs once story_suggestions.xml is provided.
        """
        from appium.webdriver.common.appiumby import AppiumBy
        SUGGESTION_MARKERS = [
            "com.instagram.android:id/follow_list_container",
            "com.instagram.android:id/suggested_users_container",
            "com.instagram.android:id/end_of_feed_demarcator",
        ]
        SUGGESTION_DESCS = ["Suggested for you", "See All"]
        try:
            for rid in SUGGESTION_MARKERS:
                if driver.find_elements(AppiumBy.ID, rid):
                    return True
            for desc in SUGGESTION_DESCS:
                if driver.find_elements(AppiumBy.XPATH, f'//*[@content-desc="{desc}"]'):
                    return True
        except Exception:
            pass
        return False

    def _is_connection_error(self, exc: Exception) -> bool:
        """Return True if exception indicates Appium server is unreachable."""
        msg = str(exc).lower()
        return any(k in msg for k in (
            "connection refused", "max retries exceeded",
            "failed to establish", "remotedisconnected",
            "session not found", "no such session",
        ))

    def _reconnect_appium(self) -> bool:
        """
        Try to restart Appium and re-establish the driver session after a crash.
        Returns True if reconnection succeeded.
        """
        self._log("🔄 Appium connection lost — attempting reconnect…")
        try:
            from src.automation.appium_manager import AppiumManager
            mgr = AppiumManager()
            mgr._ensure_server(self.appium_port, log_callback=self._log)
            self._sleep(3)
            self._controller.start_session(self.serial)
            self._log("✅ Appium reconnected.")
            return True
        except Exception as e:
            self._log(f"❌ Reconnect failed: {e}")
            return False

    def _recover_to_home(self, driver):
        """Press Back up to 5 times until reels_tray_container is visible."""
        from appium.webdriver.common.appiumby import AppiumBy
        for _ in range(5):
            try:
                if driver.find_elements(
                    AppiumBy.XPATH, '//*[@content-desc="reels_tray_container"]'
                ):
                    return
            except Exception:
                pass
            try:
                driver.back()
                self._sleep(1.0)
            except Exception:
                break

    def _open_next_unseen_story(self, driver, seen_usernames: set):
        """
        Find and open the first story slot with an active (coloured) ring.

        CONFIRMED FROM XML (tray_mixed.xml with andreacabanasd+showroomsempiterno
        unseen and pirde.moda seen):

          seen_state node: present on ALL non-own-story slots regardless of ring
                           colour — UNRELIABLE, ignored.

          avatar_image_view content-desc:
            Unseen (coloured ring): "andreacabanasd's story at column 1. Unseen."
            Seen   (grey ring):     "pirde.moda's story at column 3. Seen."

        THE ONLY RELIABLE SIGNAL: content-desc ends with "Unseen." = active ring.

        Flow:
          1. Parse page_source XML
          2. For each outer_container, read avatar_image_view content-desc
          3. Skip if ends with "Seen." (grey ring)
          4. Skip if username already opened this session
          5. Tap the lowest-column unseen slot by its outer_container bounds centre
          6. Verify we landed on story screen — recover if not
        """
        import xml.etree.ElementTree as ET, re as _re

        try:
            src  = driver.page_source or ""
            root = ET.fromstring(src)

            candidates = []
            for node in root.iter():
                if node.get("resource-id","") != "com.instagram.android:id/outer_container":
                    continue

                # Username
                uname = ""
                for child in node.iter():
                    if child.get("resource-id","") == "com.instagram.android:id/username":
                        uname = (child.get("text") or "").strip()
                        break

                if not uname or uname.lower() == "your story":
                    continue

                # THE signal: content-desc of avatar_image_view
                av_desc = ""
                col = 999
                for child in node.iter():
                    if child.get("resource-id","") == "com.instagram.android:id/avatar_image_view":
                        av_desc = child.get("content-desc","")
                        m = _re.search(r'column (\d+)', av_desc)
                        if m:
                            col = int(m.group(1))
                        break

                if av_desc.endswith("Seen."):
                    self._log(f"    ⏭️ '{uname}' — grey ring (seen)")
                    continue

                if not av_desc.endswith("Unseen."):
                    # Partially visible slot or no desc yet — skip silently
                    continue

                # Coloured ring confirmed — get tap coords from outer_container bounds
                bounds = node.get("bounds","")
                bm = _re.search(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                if not bm:
                    continue

                cx = (int(bm.group(1)) + int(bm.group(3))) // 2
                cy = (int(bm.group(2)) + int(bm.group(4))) // 2
                candidates.append((col, cx, cy, uname))

            if candidates:
                candidates.sort(key=lambda t: t[0])
                col, cx, cy, uname = candidates[0]
                driver.tap([(cx, cy)])

                # Wait up to 4s for story screen to appear (emulator/lag tolerance)
                opened = False
                for _ in range(8):
                    self._sleep(0.5)
                    if self._is_on_story_screen(driver):
                        opened = True
                        break

                if not opened:
                    self._log(f"⚠️ Tap on '{uname}' did not open story — recovering")
                    self._recover_to_home(driver)
                    return None

                self._log(f"✅ Opened story of '{uname}' (col {col}, coloured ring)")
                return uname

            self._log("ℹ️ No unseen story rings visible in tray")

        except Exception as e:
            if self._is_connection_error(e):
                raise   # let outer handler reconnect
            self._log(f"⚠️ Story tray scan error: {e}")

        return None

    def _open_first_story(self, driver) -> bool:
        """Legacy shim — delegates to _open_next_unseen_story with empty seen set."""
        result = self._open_next_unseen_story(driver, set())
        return result is not None

    def _story_like(self, driver):
        """Tap the like button. No guard — caller verified story screen."""
        from appium.webdriver.common.appiumby import AppiumBy
        try:
            like_btn = driver.find_element(
                AppiumBy.ID,
                "com.instagram.android:id/toolbar_like_button",
            )
            like_btn.click()
            self._sleep(0.5)
            self._log("    ❤️ Liked story")
        except Exception:
            try:
                size = driver.get_window_size()
                cx = int(1168 * size["width"]  / 1440.0)
                cy = int(2432 * size["height"] / 2560.0)
                driver.tap([(cx, cy)])
                self._sleep(0.5)
                self._log("    ❤️ Liked story (coord)")
            except Exception:
                pass

    def _open_story_send_bar(self, driver) -> bool:
        """Tap the send bar. No guard — caller verified story screen."""
        from appium.webdriver.common.appiumby import AppiumBy
        try:
            bar = driver.find_element(
                AppiumBy.ID,
                "com.instagram.android:id/message_composer_container",
            )
            bar.click()
            self._sleep(0.8)
            return True
        except Exception:
            self._log("⚠️ Send bar not found — skipping reaction")
            return False

    def _story_react(self, driver):
        """
        Tap one of the 4 approved reaction emoji slots in the reaction panel.

        From story_sendbar.xml: emoji slots are ImageViews with
          resource-id: com.instagram.android:id/story_reactions_emoji
          NO text, NO content-desc — must tap by scaled coordinate.

        Grid confirmed from bounds on 1440×2560 reference device:
          row1: 😂(368,618)  😮(720,618)  😍(1072,618)
          row2: 😢(368,930)  👏(720,930)  🔥(1072,930)

        We scale these reference coords to the actual screen size.
        Element-based click is skipped — el.location calls are unreliable
        mid-animation and corrupt the sort order, causing wrong slot selection.
        """
        from appium.webdriver.common.appiumby import AppiumBy
        emoji = random.choice(STORY_REACTION_EMOJIS)
        ref_cx, ref_cy = _EMOJI_COORDS_REF[emoji]

        try:
            if not self._open_story_send_bar(driver):
                self._log("⚠️ Could not open send bar for reaction")
                return

            size  = driver.get_window_size()
            tap_x = int(ref_cx * size["width"]  / 1440.0)
            tap_y = int(ref_cy * size["height"] / 2560.0)

            driver.tap([(tap_x, tap_y)])
            self._sleep(1.0)   # fixed wait — composer dismisses on its own
            self._log(f"    😊 Reacted with {emoji}")

        except Exception as exc:
            self._log(f"⚠️ React error: {exc}")

    def _story_reply(self, driver, spintax_pool, openai_key, reply_cfg):
        """
        Type a reply/comment on the current story.

        From story_sendbar.xml confirmed IDs:
          EditText:    com.instagram.android:id/reel_viewer_message_composer_text
                       (text='Send message', bounds=[48,1346][1217,1412])
          Send button: com.instagram.android:id/reel_viewer_message_composer_text_send_btn
                       (text='Send', bounds=[1217,1283][1344,1475])
        """
        from appium.webdriver.common.appiumby import AppiumBy
        try:
            if not self._open_story_send_bar(driver):
                self._log("⚠️ Could not open send bar for reply")
                return

            # Find the EditText — confirmed resource-id from XML
            reply_box = None
            for by, val in [
                (AppiumBy.ID,    "com.instagram.android:id/reel_viewer_message_composer_text"),
                (AppiumBy.XPATH, '//android.widget.EditText'),
            ]:
                try:
                    reply_box = driver.find_element(by, val)
                    break
                except Exception:
                    continue

            if reply_box is None:
                self._log("⚠️ Reply EditText not found")
                try:
                    driver.back()
                except Exception:
                    pass
                return

            text = self._compose_reply(openai_key, spintax_pool, reply_cfg)
            if not text:
                driver.back()
                return

            reply_box.clear()
            reply_box.send_keys(text)
            self._sleep(0.5)

            # Send — confirmed resource-id from XML
            try:
                send_btn = driver.find_element(
                    AppiumBy.ID,
                    "com.instagram.android:id/reel_viewer_message_composer_text_send_btn",
                )
                send_btn.click()
            except Exception:
                # Fallback: Enter key
                driver.execute_script("mobile: pressKey", {"keycode": 66})
            self._sleep(1.0)
            self._log(f"    💬 Replied: {text[:40]}")

        except Exception as exc:
            self._log(f"⚠️ Could not reply on story: {exc}")
            try:
                driver.back()
            except Exception:
                pass

    def _compose_reply(self, openai_key: str, spintax_pool: list, reply_cfg: dict) -> str:
        if openai_key:
            context = reply_cfg.get("openai_context", "Write a short friendly Instagram story reply.")
            reply = generate_openai_reply(
                api_key=openai_key,
                context=context,
                system_prompt=reply_cfg.get(
                    "openai_system_prompt",
                    "You are a friendly Instagram user. Write a short, natural reply in 1–2 sentences.",
                ),
                max_tokens=int(reply_cfg.get("openai_max_tokens", 60)),
            )
            if reply:
                return reply
        if spintax_pool:
            return spin(random.choice(spintax_pool))
        return ""

    def _advance_story(self, driver):
        try:
            size = driver.get_window_size()
            x = int(size["width"] * 0.85)
            y = size["height"] // 2
            driver.tap([(x, y)])
        except Exception:
            try:
                driver.back()
            except Exception:
                pass

    # ── Feed ───────────────────────────────────────────────────────────────────

    def _get_current_post_id(self, driver) -> str:
        """
        Return a stable unique key for the currently engaged feed post.

        The key is the poster's USERNAME — always stable, never changes when
        you like or comment. Derived from the topmost matching element so we
        always identify the post whose action bar is on screen, not a
        partially-visible post below.

        Sources in priority order (all confirmed from XML dumps):

        1. media_header_location content-desc  →  'username posted a video Xh ago'
           Extract: first word before ' posted'
           Present on: standard posts, NOT on reel-preview cards.

        2. row_feed_photo_profile_name text     →  'username'  (direct)
           Present on: standard posts, NOT on reel-preview cards.

        3. media_group content-desc            →  'Suggested Reel by Username, N likes...'
                                               or 'Reel by Username, N likes...'
           Extract: word(s) after 'by ' up to the first comma.
           Present on: ALL post types including reel-preview cards.

        4. row_feed_comment_textview_layout text → 'username comment text…'
           Extract: first whitespace-delimited token.
           Present on: posts that have comments visible below the action bar.
           Last resort — may show a commenter's name, not the poster's.
        """
        import re
        from appium.webdriver.common.appiumby import AppiumBy

        def _top_y(el) -> int:
            try:
                b = el.get_attribute("bounds") or ""
                m = re.match(r'\[(\d+),(\d+)\]', b)
                return int(m.group(2)) if m else 9999
            except Exception:
                return 9999

        def _topmost(els):
            return sorted(els, key=_top_y)

        # 1. media_header_location  →  extract username before ' posted'
        try:
            els = _topmost(driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/media_header_location",
            ))
            for el in els:
                desc = el.get_attribute("content-desc") or ""
                m = re.match(r'^([\w.\-]+)\s+posted', desc)
                if m:
                    return m.group(1).lower()
        except Exception:
            pass

        # 2. row_feed_photo_profile_name  →  direct username text
        try:
            els = _topmost(driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/row_feed_photo_profile_name",
            ))
            for el in els:
                text = (el.get_attribute("text") or "").strip()
                if text:
                    return text.lower()
        except Exception:
            pass

        # 3. media_group content-desc  →  extract after 'by '
        # e.g. 'Suggested Reel by Indiaverse, 275 likes, 18 comments, March 15'
        # e.g. 'Reel by Momna Farooq, 90 likes, 1 day ago'
        try:
            els = _topmost(driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/media_group",
            ))
            for el in els:
                desc = el.get_attribute("content-desc") or ""
                m = re.search(r'\bby\s+([^,]+)', desc, re.IGNORECASE)
                if m:
                    return m.group(1).strip().lower()
        except Exception:
            pass

        # 4. row_feed_comment_textview_layout  →  first token = commenter username
        try:
            els = _topmost(driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/row_feed_comment_textview_layout",
            ))
            for el in els:
                text = (el.get_attribute("text") or "").strip()
                if text:
                    return text.split()[0].lower()
        except Exception:
            pass

        return ""

    def _feed_action_bar_visible(self, driver) -> bool:
        """
        Return True only if the post action bar (like + comment buttons) is
        currently visible. False during reel previews, story trays, transitions.
        Confirmed from XML: row_feed_view_group_buttons is the container.
        """
        from appium.webdriver.common.appiumby import AppiumBy
        try:
            els = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/row_feed_view_group_buttons",
            )
            return bool(els)
        except Exception:
            return False

    def _do_feed(self, driver, feed_cfg: dict, reply_cfg: dict,
                 openai_key: str, spintax_pool: list):
        """
        Browse the feed, engaging with each post at most ONCE per session.

        Uses media_header_location (username + timestamp) as a stable post ID —
        unlike media_group content-desc which changes when you like a post.
        Post is marked engaged BEFORE actions fire to prevent any race condition.
        Only engages when row_feed_view_group_buttons is visible (action bar present).
        React removed — only Like and Comment supported for feed.
        """
        from appium.webdriver.common.appiumby import AppiumBy
        self._log("📰 Browsing feed…")
        try:
            self._go_home(driver)
            self._sleep(2)

            num_scrolls = int(feed_cfg.get("num_scrolls", 10))
            scroll_min  = float(feed_cfg.get("scroll_min", 1.5))
            scroll_max  = float(feed_cfg.get("scroll_max", 4.0))
            like_on     = feed_cfg.get("like_enabled", True)
            like_pct    = float(feed_cfg.get("like_pct", 40)) / 100.0
            comment_on  = feed_cfg.get("comment_enabled", False)
            comment_pct = float(feed_cfg.get("comment_pct", 5)) / 100.0

            engaged_posts: set = set()
            scrolls_done  = 0

            while scrolls_done < num_scrolls and not self._stop_flag:
                post_id = self._get_current_post_id(driver)

                if post_id and post_id in engaged_posts:
                    # Same post still on screen — scroll only, no re-engagement
                    self._feed_scroll(driver)
                    self._sleep(random.uniform(scroll_min, scroll_max))
                    scrolls_done += 1
                    continue

                if self._feed_action_bar_visible(driver):
                    # Mark BEFORE actions — liking changes DOM/desc
                    if post_id:
                        engaged_posts.add(post_id)
                    if like_on and random.random() < like_pct:
                        self._feed_like_visible(driver)
                    if comment_on and random.random() < comment_pct:
                        self._feed_comment_visible(driver, spintax_pool, openai_key, reply_cfg)

                self._feed_scroll(driver)
                self._sleep(random.uniform(scroll_min, scroll_max))
                scrolls_done += 1

            self._log(f"✅ Feed session done ({scrolls_done} scrolls).")
        except Exception as exc:
            self._log(f"⚠️ Feed error: {exc}")

    def _feed_like_visible(self, driver):
        """
        Like the current feed post.
        Confirmed from XML: resource-id=row_feed_button_like, content-desc='Like'.
        content-desc='Unlike' means already liked — skip silently.
        """
        from appium.webdriver.common.appiumby import AppiumBy
        try:
            like_btn = driver.find_elements(
                AppiumBy.XPATH,
                '//*[@resource-id="com.instagram.android:id/row_feed_button_like" '
                'and @content-desc="Like"]',
            )
            if like_btn:
                like_btn[0].click()
                self._sleep(0.5)
                self._log("  ❤️ Liked a feed post.")
            # Silent skip if not found or already liked — not an error
        except Exception as e:
            self._log(f"  ⚠️ Like error: {e}")

    def _feed_comment_visible(self, driver, spintax_pool, openai_key, reply_cfg):
        """
        Open comment sheet, post ONE comment, close sheet cleanly.

        Confirmed IDs from XML:
          Comment button: com.instagram.android:id/row_feed_button_comment
          EditText:       com.instagram.android:id/layout_comment_thread_edittext
          Post button:    com.instagram.android:id/layout_comment_thread_post_button_click_area

        StaleElementReference fix: after edittext.click() IG re-renders DOM
        (keyboard slides up) — must re-find edittext before send_keys.

        Sheet stays open after posting — _close_comment_sheet polls until gone.
        """
        from appium.webdriver.common.appiumby import AppiumBy

        text = self._compose_reply(openai_key, spintax_pool, reply_cfg)
        if not text:
            return

        comment_sheet_opened = False
        try:
            comment_btn = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/row_feed_button_comment",
            )
            if not comment_btn:
                return  # action bar gone between check and click — silent skip

            comment_btn[0].click()
            comment_sheet_opened = True
            self._sleep(1.5)

            edittext_els = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/layout_comment_thread_edittext",
            )
            if not edittext_els:
                self._log("  ⚠️ Comment edittext not found.")
                self._close_comment_sheet(driver)
                return

            # Click to focus — this invalidates the element reference (DOM re-renders)
            edittext_els[0].click()
            self._sleep(0.5)

            # Re-find after DOM re-render, then send_keys
            edittext_els2 = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/layout_comment_thread_edittext",
            )
            if not edittext_els2:
                self._log("  ⚠️ Comment edittext gone after focus.")
                self._close_comment_sheet(driver)
                return

            edittext_els2[0].send_keys(text)
            self._sleep(0.5)

            post_btn = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/layout_comment_thread_post_button_click_area",
            )
            if post_btn:
                post_btn[0].click()
            else:
                driver.execute_script("mobile: pressKey", {"keycode": 66})

            self._sleep(1.5)
            self._log(f"  💬 Feed comment posted: {text[:40]}")

        except Exception as e:
            self._log(f"  ⚠️ Comment error: {e}")

        finally:
            if comment_sheet_opened:
                self._close_comment_sheet(driver)

    def _close_comment_sheet(self, driver):
        """
        Press Back until the comment bottom sheet is fully dismissed.
        Handles keyboard-open (2 presses) and keyboard-closed (1 press) states.
        Polls layout_comment_thread_parent disappearance (max 3 attempts).
        """
        from appium.webdriver.common.appiumby import AppiumBy
        for _ in range(3):
            try:
                sheet = driver.find_elements(
                    AppiumBy.ID,
                    "com.instagram.android:id/layout_comment_thread_parent",
                )
                if not sheet:
                    return
            except Exception:
                return
            try:
                driver.back()
                self._sleep(0.8)
            except Exception:
                return

    def _feed_scroll(self, driver):
        try:
            size = driver.get_window_size()
            w, h = size["width"], size["height"]
            driver.swipe(
                start_x=w // 2, start_y=int(h * 0.75),
                end_x=w // 2,   end_y=int(h * 0.25),
                duration=random.randint(400, 900),
            )
        except Exception:
            pass

    # ── Reels helpers ─────────────────────────────────────────────────────────

    def _reel_like(self, driver):
        """
        Like the current reel using the vertical right-side toolbar.

        From XML the like button is:
          resource-id="com.instagram.android:id/like_button"
          content-desc="Like"   (when not yet liked)
          content-desc="Unlike" (already liked — skip)
        Bounds in the XML: [1232,1165][1408,1341]
        """
        from appium.webdriver.common.appiumby import AppiumBy
        try:
            # Primary: resource-id like_button with content-desc "Like"
            btns = driver.find_elements(
                AppiumBy.XPATH,
                '//*[@resource-id="com.instagram.android:id/like_button" '
                'and @content-desc="Like"]',
            )
            if btns:
                btns[0].click()
                self._sleep(0.6)
                self._log("  ❤️ Liked a reel.")
                return

            # Fallback: content_appreciation_button (double-tap-to-like area)
            # present on some IG versions — bounds [64,1764][392,1868] in XML
            btns2 = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/content_appreciation_button",
            )
            if btns2:
                btns2[0].click()
                self._sleep(0.6)
                self._log("  ❤️ Liked a reel (appreciation button).")
        except Exception as e:
            self._log(f"  ⚠️ Reel like error: {e}")

    def _reel_comment(self, driver, spintax_pool, openai_key, reply_cfg):
        """
        Open the comment sheet on a reel and post one comment.

        From XML the comment button group is:
          content-desc="Comment"  bounds [1232,1438][1408,1614]
        After tapping, the same comment sheet as feed opens:
          edittext: com.instagram.android:id/layout_comment_thread_edittext
          post btn: com.instagram.android:id/layout_comment_thread_post_button_click_area
        """
        from appium.webdriver.common.appiumby import AppiumBy

        text = self._compose_reply(openai_key, spintax_pool, reply_cfg)
        if not text:
            return

        comment_sheet_opened = False
        try:
            # Tap the Comment button on the right-side toolbar (content-desc="Comment")
            comment_btn = driver.find_elements(
                AppiumBy.XPATH,
                '//*[@content-desc="Comment" and @clickable="true"]',
            )
            if not comment_btn:
                return

            comment_btn[0].click()
            comment_sheet_opened = True
            self._sleep(1.5)

            edittext_els = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/layout_comment_thread_edittext",
            )
            if not edittext_els:
                self._log("  ⚠️ Reel comment edittext not found.")
                self._close_comment_sheet(driver)
                return

            edittext_els[0].click()
            self._sleep(0.5)

            # Re-find after DOM re-render from keyboard slide-up
            edittext_els2 = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/layout_comment_thread_edittext",
            )
            if not edittext_els2:
                self._log("  ⚠️ Reel comment edittext gone after focus.")
                self._close_comment_sheet(driver)
                return

            edittext_els2[0].send_keys(text)
            self._sleep(0.5)

            post_btn = driver.find_elements(
                AppiumBy.ID,
                "com.instagram.android:id/layout_comment_thread_post_button_click_area",
            )
            if post_btn:
                post_btn[0].click()
            else:
                driver.execute_script("mobile: pressKey", {"keycode": 66})

            self._sleep(1.5)
            self._log(f"  💬 Reel comment posted: {text[:40]}")

        except Exception as e:
            self._log(f"  ⚠️ Reel comment error: {e}")
        finally:
            if comment_sheet_opened:
                self._close_comment_sheet(driver)

    # ── Reels ──────────────────────────────────────────────────────────────────

    def _do_reels(self, driver, reels_cfg: dict, reply_cfg: dict,
                  openai_key: str, spintax_pool: list):
        self._log("🎬 Watching Reels…")
        try:
            self._go_reels(driver)
            self._sleep(2)

            num_reels   = int(reels_cfg.get("num_reels", 10))
            watch_min   = float(reels_cfg.get("watch_seconds_min", 5))
            watch_max   = float(reels_cfg.get("watch_seconds_max", 15))
            like_on     = reels_cfg.get("like_enabled", True)
            like_pct    = float(reels_cfg.get("like_pct", 30)) / 100.0
            comment_on  = reels_cfg.get("comment_enabled", False)
            comment_pct = float(reels_cfg.get("comment_pct", 5)) / 100.0

            for i in range(num_reels):
                if self._stop_flag:
                    break
                watch_s = random.uniform(watch_min, watch_max)
                self._log(f"  🎬 Reel {i+1}/{num_reels} — watching {watch_s:.1f}s")
                self._sleep(watch_s)
                if like_on and random.random() < like_pct:
                    self._reel_like(driver)
                if comment_on and random.random() < comment_pct:
                    self._reel_comment(driver, spintax_pool, openai_key, reply_cfg)
                # Swipe up to next reel
                self._feed_scroll(driver)
                self._sleep(random.uniform(0.5, 1.5))

            # Back to home
            try:
                driver.back()
            except Exception:
                pass
            self._log(f"✅ Reels session done ({num_reels} reels).")
        except Exception as exc:
            self._log(f"⚠️ Reels error: {exc}")

    # ── Navigation ─────────────────────────────────────────────────────────────

    def _go_home(self, driver):
        """
        Navigate to the Instagram home feed and verify the story tray is visible.
        Tries multiple strategies and retries until reels_tray_container appears,
        so the caller is guaranteed to be on the home feed regardless of where
        the app currently is (notifications, profile, explore, DMs, etc.).
        """
        from appium.webdriver.common.appiumby import AppiumBy

        def _on_home() -> bool:
            try:
                els = driver.find_elements(
                    AppiumBy.XPATH,
                    '//*[@content-desc="reels_tray_container"]',
                )
                return bool(els)
            except Exception:
                return False

        # Already on home — nothing to do
        if _on_home():
            return

        # Try tapping the Home tab in the bottom nav bar
        for _ in range(3):
            try:
                home_btn = driver.find_element(
                    AppiumBy.XPATH,
                    '//*[contains(@resource-id,"feed_tab") or '
                    '@content-desc="Home" or @content-desc="Feed"]',
                )
                home_btn.click()
                self._sleep(1.5)
                if _on_home():
                    return
            except Exception:
                pass
            # Fall back to pressing Back to dismiss modals/sheets
            try:
                driver.back()
                self._sleep(1.0)
            except Exception:
                pass
            if _on_home():
                return

        # Last resort: press Back up to 5 more times until tray appears
        for _ in range(5):
            if _on_home():
                return
            try:
                driver.back()
                self._sleep(1.0)
            except Exception:
                break

        if not _on_home():
            self._log("⚠️ Could not confirm home feed — proceeding anyway")

    def _go_reels(self, driver):
        from appium.webdriver.common.appiumby import AppiumBy
        try:
            reels_btn = driver.find_element(
                AppiumBy.XPATH,
                '//*[contains(@resource-id,"clips_tab") or '
                '@content-desc="Reels" or @content-desc="Clips"]',
            )
            reels_btn.click()
            self._sleep(2)
        except Exception:
            self._log("⚠️ Could not navigate to Reels tab.")