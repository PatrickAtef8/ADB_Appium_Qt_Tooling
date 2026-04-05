"""
Instagram scraper using Appium UIAutomator2.
Navigates to target profile, opens followers/following list,
scrolls through and extracts full account data including
email, phone, location, post count, story detection.
"""
import time
import re
import random
from datetime import datetime, timedelta
from typing import Optional, Callable, List, Dict

from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException, StaleElementReferenceException
)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .appium_controller import AppiumController, _run_hidden
from src.utils.filters import extract_email, extract_phone, infer_country_code, country_code_to_name, should_skip
from src.utils.blacklist import add_to_blacklist

# ── Instagram Resource IDs ────────────────────────────────────────────────────
IG_SEARCH_TAB_LABELS = [
    "Search and explore", "Search", "Explore",
]
IG_SEARCH_BOX        = "com.instagram.android:id/action_bar_search_edit_text"
IG_USER_ROW          = "com.instagram.android:id/follow_list_container"


def _parse_count(text: str) -> int:
    """Normalize '1.2M', '15.3K', '1,234' to integer."""
    if not text:
        return 0
    text = text.replace(",", "").replace(".", "").strip()
    lower = text.lower()
    try:
        if lower.endswith("k"):
            return int(float(lower[:-1]) * 1_000)
        if lower.endswith("m"):
            return int(float(lower[:-1]) * 1_000_000)
        if lower.endswith("b"):
            return int(float(lower[:-1]) * 1_000_000_000)
        # Handle "1.2K" style (dot not stripped for floats)
        text2 = text.replace(",", "")
        lower2 = text2.lower()
        if lower2.endswith("k"):
            return int(float(lower2[:-1]) * 1_000)
        if lower2.endswith("m"):
            return int(float(lower2[:-1]) * 1_000_000)
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else 0
    except (ValueError, TypeError):
        return 0


def _rand(a: float, b: float) -> float:
    """Random float between a and b."""
    return random.uniform(min(a, b), max(a, b))


def _parse_ig_date(text: str) -> Optional[datetime]:
    """
    Parse Instagram date strings like:
    - '2 hours ago', '5 days ago', '1 week ago'
    - 'July 5, 2024', 'July 5', '5 July'
    - '23 October 2023'
    Returns a datetime object or None.
    """
    if not text: return None
    # Clean up the text: remove "See translation", dots, and extra whitespace
    text = text.split("•")[0].split("·")[0].strip().lower()
    now = datetime.now()
    
    try:
        # Relative dates
        m = re.search(r"(\d+)\s+(minute|hour|day|week)s?\s+ago", text)
        if m:
            val = int(m.group(1))
            unit = m.group(2)
            if "minute" in unit: return now - timedelta(minutes=val)
            if "hour" in unit: return now - timedelta(hours=val)
            if "day" in unit: return now - timedelta(days=val)
            if "week" in unit: return now - timedelta(weeks=val)
            
        # Absolute dates
        # Try common formats
        # Instagram often uses "Month Day, Year" or "Day Month Year"
        # We need to handle "July 5, 2024" -> "July 5 2024" for easier parsing
        clean_abs = re.sub(r"[,]", "", text).title()
        for fmt in ["%B %d %Y", "%B %d", "%d %B %Y", "%d %B"]:
            try:
                dt = datetime.strptime(clean_abs, fmt)
                if dt.year == 1900: # No year in string
                    dt = dt.replace(year=now.year)
                    if dt > now: dt = dt.replace(year=now.year - 1)
                return dt
            except: continue
            
    except: pass
    return None


class InstagramScraper:
    def __init__(
        self,
        controller: AppiumController,
        on_account_found: Optional[Callable[[dict], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        on_switch_check: Optional[Callable[[int], None]] = None,
    ):
        self.ctrl = controller
        self.on_account_found = on_account_found
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_switch_check = on_switch_check
        self._stop_flag = False
        self._need_reopen_list = False   # set True after account switch to force re-navigation

    def stop(self):
        self._stop_flag = True

    def _log(self, msg: str):
        if self.on_log:
            self.on_log(msg)

    def _find(self, by, value, timeout=10):
        return WebDriverWait(self.ctrl.driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )

    def _find_all(self, by, value):
        return self.ctrl.driver.find_elements(by, value)

    # ── Navigation ────────────────────────────────────────────────────────────

    def navigate_to_profile(self, username: str) -> bool:
        """Navigate to a profile via the Search tab (safe, human-like).
        Falls back to ADB deep link only if search fails entirely."""
        driver = self.ctrl.driver
        self._log(f"Looking up @{username}...")

        # Strategy 1: Search tab (primary — avoids detectable deep-link jumps)
        self._log("Opening search...")
        search_clicked = False
        for label in IG_SEARCH_TAB_LABELS:
            try:
                el = driver.find_element(AppiumBy.ACCESSIBILITY_ID, label)
                el.click()
                time.sleep(2)
                search_clicked = True
                break
            except NoSuchElementException:
                continue

        if not search_clicked:
            try:
                driver.find_element(
                    AppiumBy.ANDROID_UIAUTOMATOR,
                    'new UiSelector().descriptionContains("earch")'
                ).click()
                time.sleep(2)
                search_clicked = True
            except Exception:
                self._log("❌ Search tab not available")
                return False

        # ── Find and activate the search box ─────────────────────────────────
        # After clicking the search tab, IG sometimes lands on an Explore/Reels
        # page where the EditText isn't immediately visible. We retry up to 4
        # times, re-clicking the search icon each time, before giving up.
        search_box = None
        for attempt in range(4):
            try:
                search_box = self._find(AppiumBy.ANDROID_UIAUTOMATOR,
                    'new UiSelector().className("android.widget.EditText")', timeout=4)
                search_box.click()
                time.sleep(0.5)
                break
            except Exception:
                self._log(f"⏳ Waiting for search box... (attempt {attempt + 1}/4)")
                # Try clicking the search bar / magnifier that may be visible
                for retry_label in IG_SEARCH_TAB_LABELS:
                    try:
                        driver.find_element(AppiumBy.ACCESSIBILITY_ID, retry_label).click()
                        time.sleep(1.5)
                        break
                    except Exception:
                        pass
                # Also try tapping a search bar placeholder text
                for placeholder in ("Search", "Search…", "Search Instagram"):
                    try:
                        driver.find_element(
                            AppiumBy.ANDROID_UIAUTOMATOR,
                            f'new UiSelector().text("{placeholder}").clickable(true)'
                        ).click()
                        time.sleep(1.5)
                        break
                    except Exception:
                        pass
                time.sleep(1)

        if search_box is None:
            self._log("❌ Search box not responding after several attempts")
            return False

        try:
            search_box.clear()
            search_box.send_keys(username)
            time.sleep(3)
        except Exception as e:
            self._log(f"❌ Search box error: {e}")
            return False

        try:
            time.sleep(1)

            # ── Strategy A: target actual account rows by resource-id ────────
            # Instagram search shows two types of rows:
            #   1. Query-suggestion row  — magnifying glass icon + bare text
            #   2. Account row           — avatar + username + follower count
            # We must only tap account rows. The reliable signal is that an
            # account row contains a username TextView whose *parent* row also
            # contains a follower-count or subtitle element. We find all
            # account-list containers and look for an exact username match inside.
            ACCOUNT_ROW_IDS = [
                "com.instagram.android:id/unified_follow_list_user_container",
                "com.instagram.android:id/row_search_user_container",
                "com.instagram.android:id/search_result_user_container",
                "com.instagram.android:id/user_list_container",
            ]
            for row_id in ACCOUNT_ROW_IDS:
                try:
                    rows = driver.find_elements(AppiumBy.ID, row_id)
                    for row in rows:
                        try:
                            uname_el = row.find_element(
                                AppiumBy.ANDROID_UIAUTOMATOR,
                                f'new UiSelector().text("{username}")'
                            )
                            if uname_el.text.strip().lower() == username.lower():
                                self._log(f"✅ Found @{username}")
                                row.click()
                                time.sleep(4)
                                return True
                        except Exception:
                            continue
                except Exception:
                    continue

            # ── Strategy B: look after the "Accounts" section header ─────────
            # Works when IG renders a categorised results page.
            all_texts = self._find_all(AppiumBy.XPATH, '//android.widget.TextView')
            found_accounts_header = False
            for el in all_texts:
                try:
                    txt = el.text.strip()
                    if txt in ("Accounts", "People"):
                        found_accounts_header = True
                        continue
                    if found_accounts_header and txt.lower() == username.lower():
                        # Confirm this is NOT the query-suggestion row by checking
                        # that the element has no sibling with class ImageView that
                        # looks like a search/magnifying-glass icon.
                        # Simplest proxy: query-suggestion rows don't have a
                        # follower-count text nearby. We just tap and trust the
                        # section header already filtered it.
                        self._log(f"✅ Found @{username}")
                        el.click()
                        time.sleep(4)
                        return True
                except Exception:
                    continue

            # ── Strategy C: XPath targeting rows that contain BOTH the username
            #    AND a follower/subtitle text — excludes bare query-suggestion rows.
            try:
                # Find all TextViews matching the username exactly
                candidates = driver.find_elements(
                    AppiumBy.XPATH,
                    f'//android.widget.TextView[@text="{username}"]'
                )
                for candidate in candidates:
                    try:
                        # Walk up to the row container and check for a sibling
                        # TextView that contains a digit (follower count / subtitle)
                        # — query-suggestion rows have none.
                        parent = candidate.find_element(
                            AppiumBy.XPATH, "./.."
                        )
                        sibling_texts = parent.find_elements(
                            AppiumBy.CLASS_NAME, "android.widget.TextView"
                        )
                        has_account_context = any(
                            any(c.isdigit() for c in (s.text or ""))
                            or "follower" in (s.text or "").lower()
                            for s in sibling_texts
                            if s.text and s.text.strip().lower() != username.lower()
                        )
                        if has_account_context:
                            self._log(f"✅ Found @{username}")
                            candidate.click()
                            time.sleep(4)
                            return True
                    except Exception:
                        continue

                # Last resort within Strategy C: if only one result matches, it
                # must be the account (query-suggestion rows are always first and
                # would have been caught above). If multiple, skip — too risky.
                if len(candidates) == 1:
                    self._log(f"➡️ Tapping @{username}")
                    candidates[0].click()
                    time.sleep(4)
                    return True
            except Exception:
                pass

        except Exception as e:
            self._log(f"❌ Could not open profile: {e}")

        # Strategy 2: ADB deep link — last resort only (detectable, use sparingly)
        self._log("⚠️ Search failed, trying another method...")
        try:
            serial = self.ctrl._device_serial or ""
            cmd = [
                "adb", "-s", serial, "shell", "am", "start",
                "-a", "android.intent.action.VIEW",
                "-d", f"https://www.instagram.com/{username}/",
                "com.instagram.android"
            ]
            result = _run_hidden(cmd, capture_output=True, text=True, timeout=10)
            if "Error" not in result.stderr and "error" not in result.stdout.lower():
                self._log(f"✅ Opened @{username}")
                time.sleep(4)
                try:
                    driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiSelector().textContains("follower")'
                    )
                    
                    return True
                except Exception:
                    self._log("⚠️ Profile opened, but couldn't fully confirm — continuing...")
                    return True
        except Exception as e:
            self._log(f"❌ All navigation methods failed: {e}")

        return False

    def open_list(self, mode: str) -> bool:
        """Open followers or following list."""
        driver = self.ctrl.driver
        self._log(f"Opening {mode} list...")
        time.sleep(2)

        MAX_WAIT   = 30   # total seconds to keep retrying before giving up
        RETRY_WAIT = 3    # seconds to wait between attempts
        deadline   = time.time() + MAX_WAIT
        attempt    = 0

        mode_lower = mode.lower()

        STAT_ID_MAP = {
            "following": [
                "com.instagram.android:id/profile_header_following_container",
                "com.instagram.android:id/row_profile_header_following_text",
            ],
            "followers": [
                "com.instagram.android:id/profile_header_followers_container",
                "com.instagram.android:id/row_profile_header_followers_text",
            ],
        }
        STAT_FALLBACK_IDS = [
            "com.instagram.android:id/profile_header_count_container",
            "com.instagram.android:id/profile_stats_container",
        ]

        while time.time() < deadline:
            attempt += 1
            if attempt > 1:
                self._log(f"⏳ Still loading {mode} list... (attempt {attempt})")
                time.sleep(RETRY_WAIT)

            for res_id in STAT_ID_MAP.get(mode_lower, STAT_FALLBACK_IDS):
                try:
                    el = driver.find_element(AppiumBy.ID, res_id)
                    
                    el.click()
                    time.sleep(2.5)
                    return True
                except Exception:
                    continue

            try:
                all_tvs = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                for tv in all_tvs:
                    txt = tv.text.lower()
                    if mode_lower in txt and any(c.isdigit() for c in txt):
                        
                        tv.click()
                        time.sleep(2.5)
                        return True
            except Exception:
                pass

            try:
                el = driver.find_element(
                    AppiumBy.ANDROID_UIAUTOMATOR,
                    f'new UiSelector().descriptionContains("{mode}")'
                )
                
                el.click()
                time.sleep(2.5)
                return True
            except Exception:
                pass

        self._log(f"❌ Could not open {mode} list")
        return False

    def _extract_visible_accounts(self) -> List[Dict]:
        """Extract usernames (and story-ring status) from the current follower/following list screen.
        Stops at the 'Suggested for you' section boundary so suggested accounts are never scraped."""
        driver = self.ctrl.driver
        accounts = []

        # ── Guard: detect if "Suggested for you" is visible on screen ────────
        # When the real followers list ends, IG injects a "Suggested for you"
        # section. If that header is already on screen, the follower list is
        # exhausted — signal end-of-list by returning an empty list so the
        # caller's consecutive_empty counter triggers a clean stop.
        SUGGESTED_MARKERS = (
            "Suggested for you",
            "Suggested For You",
            "suggested for you",
            "People you might know",
        )
        try:
            all_tvs_check = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
            for tv in all_tvs_check:
                if tv.text.strip() in SUGGESTED_MARKERS:
                    self._log("🏁 Reached the end of the list.")
                    return []   # empty → consecutive_empty counter → clean stop
        except Exception:
            pass

        try:
            rows = driver.find_elements(AppiumBy.ID, IG_USER_ROW)
            for row in rows:
                try:
                    # ── Stop if this row belongs to the Suggested section ─────
                    # Suggested rows carry a dismiss/close button or a "Suggested"
                    # label inside them. The safest signal is a child TextView
                    # containing "Suggested" or "suggested".
                    try:
                        row_texts = [el.text for el in row.find_elements(
                            AppiumBy.CLASS_NAME, "android.widget.TextView")]
                        if any("suggested" in (t or "").lower() for t in row_texts):
                            
                            return accounts   # return what we have so far, stop here
                    except Exception:
                        pass

                    uname_el = row.find_element(AppiumBy.ID, "com.instagram.android:id/follow_list_username")
                    uname = uname_el.text.strip()
                    if uname:
                        fname = ""
                        try:
                            fname_el = row.find_element(AppiumBy.ID, "com.instagram.android:id/follow_list_subtitle")
                            fname = fname_el.text.strip()
                        except: pass

                        # ── Detect story ring directly in the list row ────────
                        has_story_in_list = False
                        try:
                            row.find_element(AppiumBy.ID, "com.instagram.android:id/reel_ring")
                            has_story_in_list = True
                        except Exception:
                            pass
                        if not has_story_in_list:
                            try:
                                avatar = row.find_element(
                                    AppiumBy.ID,
                                    "com.instagram.android:id/follow_list_avatar"
                                )
                                desc = (avatar.get_attribute("content-desc") or "").lower()
                                if "story" in desc:
                                    has_story_in_list = True
                            except Exception:
                                pass

                        accounts.append({
                            "username": uname,
                            "full_name": fname,
                            "has_story": has_story_in_list,
                        })
                except Exception:
                    continue

            if not accounts:
                all_tvs = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                for tv in all_tvs:
                    txt = tv.text.strip()
                    if txt and " " not in txt and len(txt) > 2 and len(txt) < 31:
                        if txt.lower() not in ("followers", "following", "posts", "search", "suggested"):
                            accounts.append({"username": txt, "full_name": "", "has_story": False})
        except Exception as e:
            self._log(f"⚠️ Error while reading account list: {e}")
        return accounts

    def open_profile_details(self, username: str, filters: dict = None) -> dict:
        """Click on a username to open its profile and scrape full details."""
        driver = self.ctrl.driver
        serial = self.ctrl._device_serial or ""
        details = {
            "username": username,
            "full_name": "",
            "bio": "",
            "is_private": False,
            "has_profile_pic": True,
            "post_count": 0,
            "followers": 0,
            "following": 0,
            "has_recent_post": False,
            "has_story": False,
            "email": "",
            "phone": "",
            "location": "",
            "latest_post_date_text": "",
        }

        # Track whether we actually navigated into a profile page.
        # run() must only call press_back() when this is True.
        details["_navigated_in"] = False

        try:
            # ── Tap the username row in the following/followers list ──────────
            # We MUST scope the tap to actual follow-list row containers so we
            # never accidentally tap a story ring, highlight bubble, or a
            # username that appears as a subtitle in a different row.
            FOLLOW_ROW_IDS = [
                "com.instagram.android:id/follow_list_container",
                "com.instagram.android:id/unified_follow_list_user_container",
                "com.instagram.android:id/row_user_container_base",
            ]
            USERNAME_IDS = [
                "com.instagram.android:id/follow_list_username",
                "com.instagram.android:id/row_user_primary_text",
            ]
            tapped = False
            for row_id in FOLLOW_ROW_IDS:
                if tapped:
                    break
                try:
                    rows = driver.find_elements(AppiumBy.ID, row_id)
                    for row in rows:
                        for uid in USERNAME_IDS:
                            try:
                                u_el = row.find_element(AppiumBy.ID, uid)
                                if u_el.text.strip().lower() == username.lower():
                                    u_el.click()
                                    tapped = True
                                    break
                            except Exception:
                                continue
                        if tapped:
                            break
                except Exception:
                    continue

            # Fallback: find the TextView with exactly this text, but only
            # if it is NOT inside a story/reel/highlight element.
            if not tapped:
                all_tvs = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                for tv in all_tvs:
                    if tv.text.strip().lower() != username.lower():
                        continue
                    # Reject elements whose ancestor resource-id suggests a
                    # story bubble or highlight tray
                    try:
                        parent_id = tv.find_element(
                            AppiumBy.XPATH, "./ancestor::*[@resource-id]"
                        ).get_attribute("resource-id") or ""
                        if any(x in parent_id for x in (
                            "story", "reel", "highlight", "tray"
                        )):
                            continue
                    except Exception:
                        pass
                    tv.click()
                    tapped = True
                    break

            if not tapped:
                return details   # could not find username row — stay on list

            details["_navigated_in"] = True
            time.sleep(3)

            try:
                driver.find_element(AppiumBy.ID, "com.instagram.android:id/reel_ring")
                details["has_story"] = True
            except:
                try:
                    pic = driver.find_element(AppiumBy.ID, "com.instagram.android:id/profile_header_avatar_container")
                    desc = pic.get_attribute("content-desc") or ""
                    if "Story" in desc or "story" in desc:
                        details["has_story"] = True
                except: pass

            try:
                details["full_name"] = driver.find_element(AppiumBy.ID, "com.instagram.android:id/profile_header_full_name").text.strip()
            except: pass
            try:
                bio_el  = driver.find_element(AppiumBy.ID, "com.instagram.android:id/profile_header_bio_text")
                bio_raw = bio_el.text.strip()
                # If bio is truncated Instagram appends "… more" or "... more".
                # Clicking the bio element expands it to the full text.
                if bio_raw.endswith("more") and ("…" in bio_raw or "..." in bio_raw):
                    try:
                        bio_el.click()
                        time.sleep(1)
                        bio_el  = driver.find_element(AppiumBy.ID, "com.instagram.android:id/profile_header_bio_text")
                        bio_raw = bio_el.text.strip()
                    except Exception:
                        pass
                details["bio"] = bio_raw
            except: pass

            loc_ids = [
                "com.instagram.android:id/profile_header_location_text",
                "com.instagram.android:id/profile_header_business_address",
            ]
            for lid in loc_ids:
                try:
                    loc_el = driver.find_element(AppiumBy.ID, lid)
                    loc_txt = loc_el.text.strip()
                    if loc_txt:
                        details["location"] = loc_txt
                        break
                except Exception:
                    continue

            # ── Bio location fallback ─────────────────────────────────────────
            # Only runs when neither dedicated location element had a value.
            # Only the 📍 pin emoji is a reliable location signal in a bio —
            # any other heuristic risks false matches.
            # Collects ALL pin-emoji lines (there may be more than one) and
            # joins them so none are lost.
            if not details.get("location") and details.get("bio"):
                _PIN_EMOJI = "\U0001f4cd"   # 📍
                _URL_RE    = re.compile(r"(https?://|www\.|\.[a-z]{2,4}\b)", re.I)
                _EMAIL_RE  = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
                pin_lines = []
                for line in details["bio"].splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(_PIN_EMOJI):
                        candidate = line[len(_PIN_EMOJI):].strip()
                        if candidate and not _URL_RE.search(candidate) and not _EMAIL_RE.search(candidate):
                            pin_lines.append(candidate)
                if pin_lines:
                    details["location"] = "\n".join(pin_lines)

            # ── Contact button (profile header area only) ─────────────────────
            # We must NOT use a broad textContains("Contact") selector because
            # it can match DM buttons, nav items, or "Contact us" links and
            # navigate away from the profile — causing screen drift.
            # Instead, look for known resource-IDs for the contact action row,
            # and only fall back to text matching when the button text is an
            # exact known label (not a substring match).
            _CONTACT_BTN_IDS = [
                "com.instagram.android:id/profile_header_call_button",
                "com.instagram.android:id/profile_header_email_button",
                "com.instagram.android:id/profile_header_contact_button",
                "com.instagram.android:id/contact_button",
            ]
            _EXACT_CONTACT_LABELS = {"contact", "email", "call", "text", "whatsapp"}
            contact_btn = None
            # Try by resource-id first (most reliable)
            for _cid in _CONTACT_BTN_IDS:
                try:
                    contact_btn = driver.find_element(AppiumBy.ID, _cid)
                    break
                except Exception:
                    continue
            # Fall back to exact-text match scoped to profile header area
            if contact_btn is None:
                try:
                    _all_btns = driver.find_elements(
                        AppiumBy.XPATH,
                        '//android.widget.Button | //android.widget.TextView[@clickable="true"]'
                    )
                    for _btn in _all_btns:
                        _txt = (_btn.text or "").strip().lower()
                        if _txt in _EXACT_CONTACT_LABELS:
                            contact_btn = _btn
                            break
                except Exception:
                    pass

            if contact_btn is not None:
                try:
                    contact_btn.click()

                    # ── Wait for the sheet to fully load ─────────────────────
                    _CONTACT_HEADER_VARIANTS = {"contact", "call", "email", "whatsapp", "text", "phone", "mobile", "sms"}
                    for _wait_attempt in range(3):
                        time.sleep(1.0)
                        try:
                            _probe = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                            _probe_texts = [e.text.strip().lower() for e in _probe if e.text.strip()]
                            if any(t in _CONTACT_HEADER_VARIANTS for t in _probe_texts):
                                break
                        except Exception:
                            pass

                    try:
                        all_text_els = driver.find_elements(
                            AppiumBy.CLASS_NAME, "android.widget.TextView"
                        )
                        texts = [el.text.strip() for el in all_text_els if el.text.strip()]

                        sheet_start = None
                        for i, t in enumerate(texts):
                            if t.lower() == "contact":
                                sheet_start = i
                                break

                        sheet_texts = texts[sheet_start + 1:] if sheet_start is not None else texts

                        phone_labels = {"call", "text", "whatsapp", "phone", "mobile", "sms"}
                        prev = ""
                        for txt in sheet_texts:
                            if not details["email"]:
                                found_email = extract_email(txt)
                                if found_email:
                                    details["email"] = found_email
                            if not details["phone"]:
                                is_after_phone_label = prev.lower() in phone_labels
                                looks_like_phone = bool(re.search(r"\+?\d[\d\s\-\(\)]{6,}", txt))
                                starts_with_plus = txt.startswith("+")
                                if is_after_phone_label and looks_like_phone:
                                    details["phone"] = extract_phone(txt)
                                elif looks_like_phone and starts_with_plus:
                                    details["phone"] = extract_phone(txt)
                            prev = txt
                    except Exception:
                        pass

                    driver.back()
                    time.sleep(1)
                except Exception:
                    pass

            if not details["email"]:
                try:
                    # Use exact text "Email" only — textContains would also match
                    # unrelated elements and could navigate away from the profile.
                    email_btn = driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiSelector().text("Email").clickable(true)'
                    )
                    email_text = email_btn.get_attribute("content-desc") or email_btn.text
                    found = extract_email(email_text)
                    if found:
                        details["email"] = found
                except Exception:
                    pass

            try:
                all_texts = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                texts = [(el.text.strip(), el) for el in all_texts if el.text.strip()]
                for i, (txt, el) in enumerate(texts):
                    lower = txt.lower()
                    if "followers" in lower and i > 0:
                        num = re.sub(r"[^\d.KkMmBb]", "", texts[i - 1][0])
                        details["followers"] = _parse_count(num or texts[i - 1][0])
                    elif lower == "following" and i > 0:
                        num = re.sub(r"[^\d.KkMmBb]", "", texts[i - 1][0])
                        details["following"] = _parse_count(num or texts[i - 1][0])
                    elif lower == "posts" and i > 0:
                        num = re.sub(r"[^\d.KkMmBb]", "", texts[i - 1][0])
                        details["post_count"] = _parse_count(num or texts[i - 1][0])
            except Exception:
                pass

            try:
                driver.find_element(
                    AppiumBy.XPATH,
                    '//*[contains(@text,"This account is private") or '
                    'contains(@text,"Account is private") or '
                    'contains(@content-desc,"This account is private")]'
                )
                details["is_private"] = True
            except Exception:
                try:
                    driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiSelector().textContains("This account is private")'
                    )
                    details["is_private"] = True
                except Exception:
                    details["is_private"] = False

            try:
                # IG 278.0: content-desc="Profile photo" is identical for real
                # pic vs default silhouette. Only pixel colour tells them apart.
                # Strategy (in order of reliability):
                #   1. Appium element.rect — exact pixel bounds
                #   2. page_source bounds attribute
                #   3. Proportional fallback from screen size (better than hardcoded)

                # Get screen dimensions via ADB for proportional fallback
                _sw, _sh = 1080, 1920  # default
                try:
                    _sz = _run_hidden(
                        ["adb", "-s", serial, "shell", "wm", "size"],
                        capture_output=True, text=True, timeout=5
                    ).stdout
                    import re as _re2
                    _szm = _re2.search(r"(\d+)x(\d+)", _sz)
                    if _szm:
                        _sw, _sh = int(_szm.group(1)), int(_szm.group(2))
                except Exception:
                    pass

                # Proportional fallback: IG profile avatar is LEFT-aligned at
                # ~22% from left edge and ~27.5% from top. This formula exactly
                # reproduces the original (240,528) on 1080x1920 and scales
                # correctly for all other resolutions and DPIs.
                cx = int(_sw * 0.22)
                cy = int(_sh * 0.275)

                # Try Appium element rect first (most accurate)
                _av_found = False
                try:
                    _av = driver.find_element(
                        AppiumBy.ID,
                        "com.instagram.android:id/profile_header_avatar_container"
                    )
                    _r  = _av.rect
                    _rw, _rh = _r['width'], _r['height']
                    # Sanity check: avatar must be roughly square (not a wrapper div)
                    # and not wider than 40% of the screen
                    if _rw >= 60 and _rh >= 60 and _rw < _sw * 0.4 and (_rw / max(_rh, 1)) < 1.5:
                        cx  = _r['x'] + _rw // 2
                        cy  = _r['y'] + _rh // 2
                        _av_found = True
                except Exception as _e1:
                    # Fallback: scan all avatar-related bounds in page_source,
                    # pick the first one that looks like a square avatar (not a wrapper)
                    import re as _re
                    _src = driver.page_source or ""
                    for _m in _re.finditer(
                        r'profile_header_avatar_container[^"]*"[^>]*'
                        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        _src
                    ):
                        x1,y1,x2,y2 = int(_m.group(1)),int(_m.group(2)),int(_m.group(3)),int(_m.group(4))
                        _bw, _bh = x2 - x1, y2 - y1
                        # Accept only if: reasonably sized, roughly square, not full-width wrapper
                        if _bw >= 60 and _bh >= 60 and _bw < _sw * 0.4 and (_bw / max(_bh, 1)) < 1.5:
                            cx, cy = (x1+x2)//2, (y1+y2)//2
                            _av_found = True
                            break

                # ── helpers ──────────────────────────────────────────────────
                import subprocess, struct, zlib

                def parse_png_pixel(data, px, py):
                    """Return (R,G,B) at pixel (px,py) from raw PNG bytes."""
                    pos = 8
                    width = height = 0
                    color_type = 6  # default RGBA
                    idat_chunks = []
                    while pos < len(data) - 12:
                        length = struct.unpack('>I', data[pos:pos+4])[0]
                        ctype  = data[pos+4:pos+8]
                        cdata  = data[pos+8:pos+8+length]
                        if ctype == b'IHDR':
                            width      = struct.unpack('>I', cdata[0:4])[0]
                            height     = struct.unpack('>I', cdata[4:8])[0]
                            color_type = cdata[9]
                        elif ctype == b'IDAT':
                            idat_chunks.append(cdata)
                        elif ctype == b'IEND':
                            break
                        pos += 12 + length
                    if not idat_chunks or width == 0:
                        return None
                    bpp = 4 if color_type == 6 else 3  # RGBA=4, RGB=3
                    raw_img   = zlib.decompress(b''.join(idat_chunks))
                    stride    = 1 + width * bpp
                    row_start = py * stride + 1 + px * bpp
                    r, g, b   = raw_img[row_start], raw_img[row_start+1], raw_img[row_start+2]
                    return r, g, b

                def take_screencap():
                    """Return raw PNG bytes from device, or b'' on failure."""
                    res = _run_hidden(
                        ["adb", "-s", serial, "shell", "screencap", "-p"],
                        capture_output=True, text=False, timeout=10
                    )
                    # NOTE: do NOT replace b'\r\n' → b'\n' here.
                    # screencap -p is raw binary PNG; byte-mangling corrupts IDAT.
                    return res.stdout if res.stdout else b""

                def sample_avatar(raw_png):
                    """
                    Sample 5 pixels around (cx, cy) and return
                    (has_pic, cr, cg, cb, variance) or None if PNG unreadable.
                    """
                    if raw_png[:4] != b'\x89PNG':
                        return None
                    offsets = [(0,0),(8,0),(-8,0),(0,8),(0,-8)]
                    colors = []
                    for dx, dy in offsets:
                        c = parse_png_pixel(raw_png, cx+dx, cy+dy)
                        if c:
                            colors.append(c)
                    if not colors:
                        return None
                    cr, cg, cb = colors[0]
                    r_vals   = [c[0] for c in colors]
                    variance = max(r_vals) - min(r_vals)
                    is_grey    = abs(int(cr)-int(cg)) < 20 and abs(int(cr)-int(cb)) < 20
                    is_uniform = variance < 20
                    has_p = not (is_grey and is_uniform)
                    return has_p, cr, cg, cb, variance, is_grey, is_uniform

                # ── take screencap + sample (with one retry for load timing) ──
                has_pic = True  # safe fallback
                try:
                    raw = take_screencap()
                    result_tuple = sample_avatar(raw)

                    if result_tuple is None:
                        pass  # defaulting has_pic = True (safe fallback)
                    else:
                        has_p, cr, cg, cb, variance, is_grey, is_uniform = result_tuple
                        has_pic = has_p
                except Exception as pe:
                    pass  # PNG parse error — keep has_pic = True fallback

                details["has_profile_pic"] = has_pic
            except Exception as pic_ex:
                details["has_profile_pic"] = True

            # ── Bio contact extraction ────────────────────────────────────────
            # If the Contact button didn't yield an email/phone, scan the bio
            # text directly. This covers accounts that put contact details in
            # the bio without a Contact button.
            if not details["email"] and details.get("bio"):
                _bio_email = extract_email(details["bio"])
                if _bio_email:
                    details["email"] = _bio_email
            if not details["phone"] and details.get("bio"):
                # Only accept international-format numbers from bio (starts with +)
                # to avoid grabbing follower counts or other digit strings.
                _bio_phone_m = re.search(r"\+\d[\d\s\-\(\)]{6,}", details["bio"])
                if _bio_phone_m:
                    details["phone"] = extract_phone(_bio_phone_m.group(0))

            # Accounts with 0 posts cannot have an "old" post — the post-age
            # filter must never reject them.  Set has_recent_post=True
            # unconditionally here; it will be overwritten to False below
            # only when the post-spin actually finds and checks a real post.
            if not details["is_private"]:
                details["has_recent_post"] = True

            if not details["is_private"] and details["post_count"] > 0:
                # ── Early-exit guard — post-date spin is the LAST check ───────
                # Evaluate every cheap filter condition BEFORE doing the
                # expensive post-date check (opening a post, reading its date).
                # If the account would already be skipped for ANY other reason,
                # skip the post-spin entirely.
                _skip_early = False
                if filters:
                    # 1. Private account
                    if not _skip_early and filters.get("skip_private", False):
                        if details.get("is_private", False):
                            _skip_early = True

                    # 2. No bio
                    if not _skip_early and filters.get("skip_no_bio", False):
                        if not details.get("bio", "").strip():
                            _skip_early = True

                    # 3. No profile picture
                    if not _skip_early and filters.get("skip_no_profile_pic", False):
                        if not details.get("has_profile_pic", True):
                            _skip_early = True

                    # 4. Keyword blacklist
                    if not _skip_early:
                        _kws = filters.get("keywords", [])
                        if _kws:
                            _haystack = " ".join([
                                details.get("username", ""),
                                details.get("full_name", ""),
                                details.get("bio", ""),
                            ]).lower()
                            if any(kw.strip().lower() and kw.strip().lower() in _haystack for kw in _kws):
                                _skip_early = True

                    # 5. Minimum posts
                    if not _skip_early:
                        _min_posts = int(filters.get("min_posts", 0))
                        if _min_posts > 0 and details.get("post_count", 0) < _min_posts:
                            _skip_early = True

                    # 6. No contact info — skip only when NEITHER bio NOR
                    #    Contact button yielded an email OR a phone number.
                    if not _skip_early and filters.get("skip_no_contact", False):
                        _has_email = bool(details.get("email", "").strip())
                        _has_phone = bool(details.get("phone", "").strip())
                        if not _has_email and not _has_phone:
                            _skip_early = True

                enable_spin = (filters.get("enable_post_spin", False) if filters else False) and not _skip_early
                months_threshold = int(filters.get("skip_no_posts_last_n_months", 0)) if filters else 0

                # ── Story-ring short-circuit ──────────────────────────────────
                if details.get("has_story"):
                    details["has_recent_post"] = True
                    if enable_spin and months_threshold > 0:
                        self._log(f"⚡ @{username} is active (has a story)")
                    enable_spin = False   # prevent the block below from running

                if enable_spin and months_threshold > 0:
                    try:
                        self._log(f"🔍 Checking last post date for @{username}...")
                        
                        post_el = None
                        grid_post_selectors = [
                            (AppiumBy.XPATH, "//android.widget.Button[contains(@content-desc, 'row 1, column 1')]"),
                            (AppiumBy.XPATH, "//android.widget.ImageView[contains(@content-desc, 'row 1, column 1')]"),
                            (AppiumBy.XPATH, "//android.widget.Button[contains(@content-desc, 'Post by')]"),
                            (AppiumBy.XPATH, "//android.widget.ImageView[contains(@content-desc, 'Post by')]"),
                            (AppiumBy.ID, "com.instagram.android:id/image_button"),
                            (AppiumBy.ID, "com.instagram.android:id/media_set_row_content_1"),
                        ]
                        
                        for attempt_find in range(3):
                            for by, val in grid_post_selectors:
                                try:
                                    els = driver.find_elements(by, val)
                                    if els:
                                        # Validate: reject highlight/story elements
                                        for candidate in els:
                                            desc = (candidate.get_attribute("content-desc") or "").lower()
                                            # Must NOT be a highlight or story bubble
                                            if "highlight" in desc or "story" in desc:
                                                continue
                                            post_el = candidate
                                            
                                            break
                                        if post_el:
                                            break
                                except: continue
                            if post_el: break
                            
                            # Fallback: Try to find a grid post button/image with strict validation
                            try:
                                all_clickable = driver.find_elements(AppiumBy.XPATH, "//android.widget.Button | //android.widget.ImageView")
                                for el in all_clickable:
                                    desc = (el.get_attribute("content-desc") or "").lower()
                                    # Must reference a grid row/column position
                                    has_grid_position = ("row 1" in desc and "column 1" in desc)
                                    # Must be an actual post — not a highlight or story bubble
                                    is_story_or_highlight = "highlight" in desc or "story" in desc
                                    # Must explicitly be a post reference
                                    is_post = "post by" in desc or has_grid_position
                                    if is_post and not is_story_or_highlight:
                                        post_el = el
                                        
                                        break
                            except: pass
                            if post_el: break

                            if attempt_find < 2:
                                self._log(f"📜 Date not visible yet, scrolling... (attempt {attempt_find+1})")
                                # Use Appium swipe (session-aware) instead of ADB shell
                                # swipe to avoid accidentally triggering stories/reels
                                # on the profile page during a blind coordinate swipe.
                                try:
                                    _sz = driver.get_window_size()
                                    _w, _h = _sz["width"], _sz["height"]
                                    driver.swipe(_w // 2, int(_h * 0.65),
                                                 _w // 2, int(_h * 0.35), 600)
                                except Exception:
                                    pass
                                time.sleep(2.0)
                        
                        if post_el:
                            try:
                                post_el.click()
                            except:
                                # Final fallback: Coordinate-based click if element click fails
                                
                                loc = post_el.location
                                size = post_el.size
                                cx, cy = loc['x'] + size['width'] // 2, loc['y'] + size['height'] // 2
                                _run_hidden(["adb", "-s", self.ctrl._device_serial or "", "shell", "input", "tap", str(cx), str(cy)])
                            
                            time.sleep(3.0)

                            # ── Guard: detect accidental highlight / story viewer ──────────
                            # If we tapped a highlight bubble instead of a grid post, the UI
                            # will show a story-style viewer (no date element, progress bars
                            # at the top, or a "Highlight" label). Detect and escape cleanly.
                            landed_in_story = False
                            try:
                                # Story/highlight viewers have a progress-bar strip at top
                                story_indicators = driver.find_elements(
                                    AppiumBy.XPATH,
                                    '//*[contains(@resource-id,"reel_viewer_progress") '
                                    'or contains(@resource-id,"story_progress") '
                                    'or contains(@resource-id,"highlight_title") '
                                    'or contains(@content-desc,"Highlight") '
                                    'or contains(@content-desc,"highlight")]'
                                )
                                if story_indicators:
                                    landed_in_story = True
                                    self._log(f"⚠️ Tapped a story instead of a post for @{username} — going back")
                            except Exception:
                                pass

                            if landed_in_story:
                                driver.back()
                                time.sleep(1.5)
                                
                                details["latest_post_date_text"] = ""
                                # Treat as "date unknown" — do NOT mark has_recent_post False
                                # just skip the date-based filter for this account
                                details["has_recent_post"] = True
                                return details
                            # ──────────────────────────────────────────────────────────────

                            date_text = ""
                            date_ids = [
                                "com.instagram.android:id/post_date",
                                "com.instagram.android:id/feed_post_header_timestamp",
                            ]
                            
                            date_regex = r"(\d+\s+(day|hour|minute|week)s?\s+ago)|(^[A-Z][a-z]+\s+\d+)|(^\d+\s+[A-Z][a-z]+)"
                            # Reject anything that looks like an engagement count, not a date
                            non_date_regex = r"^\s*[\d,\.]+\s*(like|view|comment|share|save)s?\s*$"

                            def _is_valid_date_text(txt: str) -> bool:
                                """Return True only if txt looks like a date, not a likes/views count."""
                                if not txt:
                                    return False
                                # Reject engagement counts (e.g. "140 likes", "1,234 views")
                                if re.search(non_date_regex, txt, re.I):
                                    return False
                                # Must match the date pattern
                                return bool(re.search(date_regex, txt, re.I))

                            for attempt in range(4):
                                for d_id in date_ids:
                                    try:
                                        date_el = driver.find_element(AppiumBy.ID, d_id)
                                        txt = date_el.text.strip()
                                        if _is_valid_date_text(txt):
                                            date_text = txt
                                            break
                                        elif txt:
                                            pass  # non-date text, skip
                                    except: continue
                                if date_text: break
                                
                                all_tvs = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                                for tv in reversed(all_tvs):
                                    txt = tv.text.strip()
                                    if _is_valid_date_text(txt):
                                        date_text = txt
                                        break
                                if date_text: break
                                
                                self._log(f"📜 Date not visible yet, scrolling... (attempt {attempt+1})")
                                # Safe Appium swipe — does not fire ADB input events
                                # that could accidentally exit the post view.
                                try:
                                    _sz2 = driver.get_window_size()
                                    _w2, _h2 = _sz2["width"], _sz2["height"]
                                    driver.swipe(_w2 // 2, int(_h2 * 0.70),
                                                 _w2 // 2, int(_h2 * 0.30), 600)
                                except Exception:
                                    pass
                                time.sleep(2)
                            
                            if date_text:
                                self._log(f"📅 @{username} last posted: {date_text}")
                                details["latest_post_date_text"] = date_text
                                
                                # Perform skipping logic
                                post_dt = _parse_ig_date(date_text)
                                if post_dt:
                                    now = datetime.now()
                                    # Use exact day-accurate age in months (30.44 days/month)
                                    age_days = (now - post_dt).days
                                    age_months_exact = age_days / 30.44
                                    # Only skip if the post is STRICTLY OLDER than the threshold
                                    # e.g. threshold=1 → skip only if age > 1 full month (>30 days)
                                    if age_months_exact > months_threshold:
                                        self._log(f"⏭️ @{username}: last post was {age_months_exact:.1f} months ago — skipping")
                                        details["has_recent_post"] = False
                                    else:
                                        self._log(f"✅ @{username}: recently active ({age_months_exact:.1f} months ago)")
                                else:
                                    self._log(f"⚠️ Couldn't read post date for @{username}, continuing anyway")
                            else:
                                self._log(f"⚠️ Could not find post date for @{username}")
                            
                            
                            driver.back()
                            time.sleep(1.5)
                            
                            if details.get("has_recent_post") == False:
                                
                                return details
                        else:
                            self._log(f"⚠️ No posts found on @{username}'s profile")
                    except Exception as e:
                        self._log(f"⚠️ Could not check post date for @{username}: {e}")

            details["country_code"] = infer_country_code(
                details["phone"], details["location"]
            )

            # If location is empty but we detected a country from the phone number,
            # fill location with the full country name derived from the country code.
            # Note: infer_country_code already checks location text first internally,
            # so country_code here reflects location keywords if any were found.
            # We only fill location from country_code when location is truly empty.
            if not details.get("location") and details.get("country_code"):
                details["location"] = country_code_to_name(details["country_code"])

        except Exception as e:
            self._log(f"❌ Could not open @{username}'s profile: {e}")

        return details

    def _appium_navigate_to_home(self) -> bool:
        """Clear the back stack and return to main activity."""
        driver = self.ctrl.driver
        serial = self.ctrl._device_serial or ""
        try:
            LIST_IDS = [
                "com.instagram.android:id/follow_list_container",
                "com.instagram.android:id/row_user_container_base",
                "com.instagram.android:id/unified_follow_list_user_container",
            ]
            if driver is not None:
                for _ in range(6):
                    list_found = False
                    for lid in LIST_IDS:
                        try:
                            driver.find_element(AppiumBy.ID, lid)
                            list_found = True
                            break
                        except NoSuchElementException:
                            continue
                    if not list_found:
                        break
                    driver.back()
                    time.sleep(1.2)

            _run_hidden(
                [
                    "adb", "-s", serial, "shell", "am", "start",
                    "--activity-clear-top",
                    "-n", "com.instagram.android/com.instagram.mainactivity.InstagramMainActivity",
                ],
                capture_output=True, text=True, timeout=10,
            )
            time.sleep(2.0)
            
            return True
        except Exception as e:
            self._log(f"⚠️ Navigation error: {e}")
            return False

    # ── Screen verification & recovery ───────────────────────────────────────

    def _verify_on_list(self) -> bool:
        """
        Return True if the screen is currently showing a followers/following
        list (i.e. we see the expected row containers).  False means we have
        drifted somewhere else — a story viewer, Reels, DMs, home feed, etc.

        Retries up to 3 times with a short sleep to absorb Instagram's page
        transition animations, so a momentarily blank screen between profile
        visits does not trigger a false-positive recovery.
        """
        LIST_ROW_IDS = [
            "com.instagram.android:id/follow_list_container",
            "com.instagram.android:id/row_user_container_base",
            "com.instagram.android:id/unified_follow_list_user_container",
            "com.instagram.android:id/follow_list_sorting_option_container",
        ]
        for _retry in range(3):
            try:
                driver = self.ctrl.driver
                for lid in LIST_ROW_IDS:
                    try:
                        els = driver.find_elements(AppiumBy.ID, lid)
                        if els:
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            if _retry < 2:
                time.sleep(1.0)   # wait for page transition to complete
        return False

    def _recover_to_list(self, target_username: str, mode: str) -> bool:
        """
        Full recovery when the scraper has drifted to a wrong screen
        (story viewer, Reels, DMs, accidental tap, etc.).

        Strategy (in order of escalation):
          1. If we detect a story/reel viewer, press Back once first.
          2. Try up to 4 regular Back presses to unwind the back-stack.
          3. If still not on the list, force-stop Instagram via ADB and
             relaunch it, then re-navigate to the target profile and list.

        Returns True if we successfully land back on the followers/following
        list, False if all attempts fail (caller should stop the run).
        """
        serial = self.ctrl._device_serial or ""
        driver = self.ctrl.driver
        self._log("⚠️ Screen drift detected — attempting recovery...")

        # ── Step 1: escape story/reel viewer with a single Back ──────────────
        try:
            story_ids = [
                "com.instagram.android:id/reel_viewer_progress_container",
                "com.instagram.android:id/reel_viewer_root",
                "com.instagram.android:id/story_progress_container",
                "com.instagram.android:id/clips_viewer_fragment_container",
            ]
            for sid in story_ids:
                try:
                    if driver.find_elements(AppiumBy.ID, sid):
                        self._log("↩️ Escaping story/reel viewer...")
                        driver.back()
                        time.sleep(1.5)
                        break
                except Exception:
                    continue
        except Exception:
            pass

        # ── Step 2: try up to 4 Back presses to unwind ───────────────────────
        for i in range(4):
            if self._verify_on_list():
                self._log("✅ Recovery successful (back-press).")
                return True
            try:
                driver.back()
                time.sleep(1.2)
            except Exception:
                break

        if self._verify_on_list():
            self._log("✅ Recovery successful (back-press).")
            return True

        # ── Step 3: soft recovery — Home → relaunch Instagram → Search ───────
        # Press Home to get out of whatever stuck state we're in, then bring
        # Instagram back to the foreground and try to navigate normally.
        # This avoids the disruptive force-stop unless absolutely necessary.
        self._log("🏠 Pressing Home to escape stuck screen...")
        try:
            driver.press_keycode(3)   # HOME — guaranteed regardless of screen state
            time.sleep(2.0)

            # Bring Instagram back to the foreground via Appium activate_app
            driver.activate_app("com.instagram.android")
            time.sleep(3.0)

            # Dismiss any dialogs that may have appeared
            for label in ("Try Again", "OK", "Close", "Not Now", "Cancel"):
                try:
                    btn = driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        f'new UiSelector().text("{label}").clickable(true)'
                    )
                    btn.click()
                    time.sleep(0.8)
                except Exception:
                    pass

            # Try to navigate to the profile via the search tab as usual
            self._log(f"🔍 Attempting soft re-navigation to @{target_username}...")
            if self.navigate_to_profile(target_username) and self.open_list(mode):
                self._log("✅ Recovery successful (home → relaunch).")
                return True

        except Exception as e:
            self._log(f"⚠️ Soft recovery failed: {e}")

        # ── Step 4: force-stop Instagram and do a full re-navigation ─────────
        self._log("🔄 Force-stopping Instagram for full recovery...")
        try:
            _run_hidden(
                ["adb", "-s", serial, "shell", "am", "force-stop",
                 "com.instagram.android"],
                capture_output=True, text=True, timeout=10,
            )
            time.sleep(2.5)
            # Relaunch Instagram using the monkey tool — this fires the launcher
            # intent exactly as if the user tapped the app icon, works on all
            # Android versions and never opens a "recent task" instead.
            _run_hidden(
                [
                    "adb", "-s", serial, "shell", "monkey",
                    "-p", "com.instagram.android",
                    "-c", "android.intent.category.LAUNCHER",
                    "1",
                ],
                capture_output=True, text=True, timeout=10,
            )

            # ── Wait for Instagram to be the foreground app ───────────────
            # Poll until Instagram is confirmed in the foreground, trying
            # multiple ADB signals before falling back to a flat wait.
            ig_ready = False
            for _attempt in range(40):   # up to 20 s (40 × 0.5 s)
                time.sleep(0.5)
                try:
                    # Method 1: dumpsys activity (works on all Android versions)
                    act_out = _run_hidden(
                        ["adb", "-s", serial, "shell", "dumpsys", "activity",
                         "activities"],
                        capture_output=True, text=True, timeout=10,
                    ).stdout
                    if "com.instagram.android" in act_out and (
                        "mResumedActivity" in act_out or
                        "ResumedActivity" in act_out or
                        "topResumedActivity" in act_out
                    ):
                        for line in act_out.splitlines():
                            if ("mResumedActivity" in line or
                                "ResumedActivity" in line or
                                "topResumedActivity" in line) \
                                    and "com.instagram.android" in line:
                                ig_ready = True
                                break
                    if ig_ready:
                        break

                    # Method 2: check current focused package via dumpsys window
                    win_out = _run_hidden(
                        ["adb", "-s", serial, "shell", "dumpsys", "window",
                         "windows"],
                        capture_output=True, text=True, timeout=10,
                    ).stdout
                    for line in win_out.splitlines():
                        if ("mCurrentFocus" in line or "mFocusedApp" in line) \
                                and "com.instagram.android" in line:
                            ig_ready = True
                            break
                    if ig_ready:
                        break
                except Exception:
                    pass

            if not ig_ready:
                # Fallback: flat wait then try to bring Instagram forward once more
                self._log("⚠️ Instagram readiness check timed out — forcing relaunch...")
                try:
                    _run_hidden(
                        [
                            "adb", "-s", serial, "shell", "monkey",
                            "-p", "com.instagram.android",
                            "-c", "android.intent.category.LAUNCHER",
                            "1",
                        ],
                        capture_output=True, text=True, timeout=10,
                    )
                except Exception:
                    pass
                time.sleep(5.0)
            else:
                # Small extra buffer so the UI is interactive, not just foregrounded
                time.sleep(2.0)

        except Exception as e:
            self._log(f"❌ Force-stop failed: {e}")
            return False

        # Dismiss any post-launch dialogs (update prompts, rate-limit warnings)
        try:
            for _ in range(3):
                for label in ("Try Again", "OK", "Close", "Not Now", "Cancel"):
                    try:
                        btn = driver.find_element(
                            AppiumBy.ANDROID_UIAUTOMATOR,
                            f'new UiSelector().text("{label}").clickable(true)'
                        )
                        btn.click()
                        time.sleep(0.8)
                    except Exception:
                        pass
        except Exception:
            pass

        # Re-navigate to the target profile and open the list
        self._log(f"🔍 Re-navigating to @{target_username} after recovery...")
        if not self.navigate_to_profile(target_username):
            self._log("❌ Recovery failed — could not navigate to profile.")
            return False
        if not self.open_list(mode):
            self._log("❌ Recovery failed — could not open list.")
            return False

        self._log("✅ Recovery successful (full restart).")
        return True

    def scroll_list(self, swipe_distance: float = 0.6):
        """Scroll the list down using ADB shell swipe for better stability."""
        serial = self.ctrl._device_serial or ""
        try:
            # Get screen size via ADB to ensure correct coordinates
            size_out = _run_hidden(["adb", "-s", serial, "shell", "wm", "size"], capture_output=True, text=True).stdout
            m = re.search(r"(\d+)x(\d+)", size_out)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            else:
                # Fallback to standard 1080p if size detection fails
                w, h = 1080, 1920

            start_x, start_y = w // 2, int(h * 0.75)
            end_x, end_y = w // 2, int(h * (0.75 - swipe_distance))
            duration = 600
            
            _run_hidden([
                "adb", "-s", serial, "shell", "input", "swipe",
                str(start_x), str(start_y), str(end_x), str(end_y), str(duration)
            ])
        except Exception as e:
            
            try:
                driver = self.ctrl.driver
                size = driver.get_window_size()
                w, h = size["width"], size["height"]
                start_y = int(h * 0.75)
                end_y = int(h * (0.75 - swipe_distance))
                driver.swipe(w // 2, start_y, w // 2, end_y, 600)
            except Exception as e2:
                self._log(f"❌ Scroll failed: {e2}")

    def _human_warmup_scroll(self, min_seconds: int = 120, max_seconds: int = 300):
        """Simulate human-like home-feed browsing after an account switch.
        Scrolls randomly for 2-5 minutes before starting real scraping work,
        so the session looks organic to Instagram's detection systems."""
        duration = _rand(min_seconds, max_seconds)
        self._log(f"🔄 Warming up for {int(duration)}s before starting...")
        serial = self.ctrl._device_serial or ""

        # ── Navigate to Home feed before scrolling ──────────────────────────
        try:
            _run_hidden(
                [
                    "adb", "-s", serial, "shell", "am", "start",
                    "--activity-clear-top",
                    "-n", "com.instagram.android/com.instagram.mainactivity.InstagramMainActivity",
                ],
                capture_output=True, text=True, timeout=10,
            )
            time.sleep(2.5)
            # Tap the Home tab in the bottom nav bar via Appium
            driver = self.ctrl.driver
            if driver is not None:
                for home_id in (
                    "com.instagram.android:id/feed_tab",
                    "com.instagram.android:id/home_tab",
                ):
                    try:
                        el = driver.find_element(AppiumBy.ID, home_id)
                        el.click()
                        break
                    except Exception:
                        continue
                else:
                    # Fallback: tap by content-desc
                    try:
                        el = driver.find_element(AppiumBy.ACCESSIBILITY_ID, "Home")
                        el.click()
                    except Exception:
                        pass
            time.sleep(1.5)
        except Exception:
            pass
        # ────────────────────────────────────────────────────────────────────
        deadline = time.time() + duration
        while time.time() < deadline and not self._stop_flag:
            try:
                # Random swipe distance and speed for each gesture
                swipe_frac  = _rand(0.3, 0.6)
                duration_ms = int(_rand(400, 900))
                size_out = _run_hidden(
                    ["adb", "-s", serial, "shell", "wm", "size"],
                    capture_output=True, text=True
                ).stdout
                m = re.search(r"(\d+)x(\d+)", size_out)
                w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)
                start_y = int(h * _rand(0.65, 0.80))
                end_y   = int(start_y - h * swipe_frac)
                _run_hidden([
                    "adb", "-s", serial, "shell", "input", "swipe",
                    str(w // 2), str(start_y), str(w // 2), str(end_y), str(duration_ms)
                ])
            except Exception:
                pass
            # Randomised pause between gestures (1-6 s), mimicking reading time
            time.sleep(_rand(1.0, 6.0))
        self._log("✅ Ready! Starting collection...")

    def run(
        self,
        target_username: str,
        mode: str,
        max_count: int,
        filters: dict,
        delays: dict,
        fetch_details: bool = True,
        blacklist: set = None,
    ) -> int:
        """Main scraping loop."""
        if blacklist is None:
            blacklist = set()

        self._stop_flag = False
        self._need_reopen_list = False
        collected = 0
        seen_usernames = set()

        scroll_delay_min  = delays.get("between_scrolls_min",   1.0)
        scroll_delay_max  = delays.get("between_scrolls_max",   3.0)
        profile_delay_min = delays.get("between_profiles_min",  2.0)
        profile_delay_max = delays.get("between_profiles_max",  4.0)

        if not self.navigate_to_profile(target_username):
            self._log(f"❌ Could not open @{target_username}'s profile")
            return 0

        if not self.open_list(mode):
            self._log(f"❌ Could not open {mode} list for @{target_username}")
            return 0

        self._log(f"✅ Opened {mode} list. Starting collection...")
        consecutive_empty = 0
        recovery_failures = 0   # count consecutive failed recovery attempts
        mid_batch_recovered = False  # True when drift recovery happened mid-batch (skip scroll)

        while collected < max_count and not self._stop_flag:
            if self._need_reopen_list:
                self._need_reopen_list = False
                self._log(f"🔄 Switching account and re-opening {mode} list...")

                # ── Screen-state guard ───────────────────────────────────────
                # After a switch, Instagram may show an interstitial (update
                # dialog, rate-limit warning, story overlay). Dismiss it before
                # navigating, otherwise the next tap fires on the wrong element.
                try:
                    driver = self.ctrl.driver
                    for _ in range(3):
                        # Dismiss any "Try Again" / "OK" / "Close" dialogs
                        for label in ("Try Again", "OK", "Close", "Not Now", "Cancel"):
                            try:
                                btn = driver.find_element(
                                    AppiumBy.ANDROID_UIAUTOMATOR,
                                    f'new UiSelector().text("{label}").clickable(true)'
                                )
                                btn.click()
                                time.sleep(1.0)
                            except Exception:
                                pass
                except Exception:
                    pass

                if not self.navigate_to_profile(target_username):
                    self._log("❌ Account switch failed — stopping")
                    break
                if not self.open_list(mode):
                    self._log("❌ Could not reopen list after account switch — stopping")
                    break
                consecutive_empty = 0
                continue

            # ── Screen-state check: ensure we are still on the list ───────────
            # This fires every iteration so any accidental tap (story, Reels,
            # DM, etc.) is caught immediately before we try to read accounts.
            if not self._verify_on_list():
                self._log("🔍 Not on the followers/following list — recovering...")
                if self._recover_to_list(target_username, mode):
                    recovery_failures = 0
                    consecutive_empty = 0
                    continue   # restart iteration cleanly on the correct screen
                else:
                    recovery_failures += 1
                    self._log(f"❌ Recovery attempt {recovery_failures} failed.")
                    if recovery_failures >= 3:
                        self._log("❌ Too many failed recovery attempts — stopping.")
                        break
                    time.sleep(5.0)
                    continue
            else:
                recovery_failures = 0   # reset on confirmed good screen

            accounts = self._extract_visible_accounts()

            # _extract_visible_accounts returns [] with a "Suggested" log when
            # it hits the boundary. Distinguish that from a genuine empty page
            # by checking if "Suggested for you" is on screen right now.
            if not accounts:
                # Quick check: is the Suggested section visible?
                try:
                    driver_tvs = self.ctrl.driver.find_elements(
                        AppiumBy.CLASS_NAME, "android.widget.TextView"
                    )
                    if any(tv.text.strip() in (
                        "Suggested for you", "Suggested For You",
                        "suggested for you", "People you might know"
                    ) for tv in driver_tvs):
                        self._log("🏁 Reached the end of the list.")
                        break
                except Exception:
                    pass

                consecutive_empty += 1
                if consecutive_empty >= 5:
                    self._log("⚠️ No more accounts found — reached end of list")
                    break
                self.scroll_list()
                time.sleep(_rand(scroll_delay_min, scroll_delay_max))
                continue

            consecutive_empty = 0

            for acc in accounts:
                if self._stop_flag or self._need_reopen_list:
                    break
                if collected >= max_count:
                    break

                uname = acc["username"].lower()

                # ── Gate 1: already seen this scroll session (in-memory dedup) ─
                if uname in seen_usernames:
                    continue
                seen_usernames.add(uname)

                # ── Gate 2: already collected in a previous run (blacklist) ────
                # Check BEFORE opening the profile so we never waste time
                # visiting, post-spinning, or contact-scraping an account that
                # was already exported.
                if uname in blacklist:
                    self._log(f"⏭️ @{acc['username']} skipped (blacklisted)")
                    continue

                # ── Story-ring shortcut ──────────────────────────────────────
                # If the ring was already detected in the list view and no
                # contact-dependent or post-date filter is active, we can skip
                # the expensive profile visit entirely.
                acc_has_story = acc.get("has_story", False)
                need_contact  = filters.get("skip_no_contact", False)
                story_satisfies_activity = (
                    acc_has_story
                    and not need_contact
                    and int(filters.get("skip_no_posts_last_n_months", 0)) == 0
                )

                if fetch_details and not story_satisfies_activity:
                    details = self.open_profile_details(acc["username"], filters=filters)
                    # Preserve list-view story flag in case it disappeared after tap
                    if acc_has_story and not details.get("has_story"):
                        details["has_story"] = True
                    acc.update(details)
                    # Only press Back if we actually navigated into a profile.
                    # open_profile_details() sets _navigated_in=False when it
                    # could not find the username row and returned immediately,
                    # meaning the driver is still on the list — pressing Back
                    # in that case would exit the list and cause screen drift.
                    if details.get("_navigated_in", True):
                        self.ctrl.press_back()
                    time.sleep(_rand(profile_delay_min, profile_delay_max))

                    # ── Mid-batch drift guard ────────────────────────────────
                    # After pressing Back from a profile, verify we are still
                    # on the list before processing the next account in this
                    # batch. If we drifted (story viewer, Reels, accidental tap)
                    # recover now instead of silently skipping the remaining
                    # accounts in the batch.
                    if not self._verify_on_list():
                        self._log("🔍 Mid-batch drift — recovering before continuing...")
                        if self._recover_to_list(target_username, mode):
                            recovery_failures = 0
                        else:
                            recovery_failures += 1
                            self._log(f"❌ Mid-batch recovery failed ({recovery_failures}).")
                            if recovery_failures >= 3:
                                self._log("❌ Too many failed recovery attempts — stopping.")
                                self._stop_flag = True
                        mid_batch_recovered = True
                        break  # break inner for-loop; outer while restarts cleanly

                elif acc_has_story and story_satisfies_activity:
                    acc.setdefault("has_recent_post", True)
                    self._log(f"⚡ @{acc['username']} is active (has a story) — skipping profile visit")


                _skip_reason: list = []
                if should_skip(acc, filters, blacklist, _skip_reason):
                    reason_str = _skip_reason[0] if _skip_reason else "unknown filter"
                    self._log(f"⏭️ @{acc['username']} skipped ({reason_str})")
                    add_to_blacklist(acc["username"])
                    blacklist.add(uname)
                    continue

                if self.on_account_found:
                    self.on_account_found(acc)

                collected += 1

                add_to_blacklist(acc["username"])
                blacklist.add(uname)

                if self.on_progress:
                    self.on_progress(collected, max_count)

                self._log(
                    f"[{collected}/{max_count}] ✅ @{acc['username']} | "
                    f"email={acc.get('email', '-')} | "
                    f"phone={acc.get('phone', '-')} | "
                    f"country={acc.get('country_code', '-')} | "
                    f"posts={acc.get('post_count', '-')}"
                )

                if self.on_switch_check:
                    self.on_switch_check(collected)

            if not self._need_reopen_list and not mid_batch_recovered:
                self.scroll_list()
                time.sleep(_rand(scroll_delay_min, scroll_delay_max))
            mid_batch_recovered = False

        self._log(f"🏁 All done! Collected {collected} account(s).")
        return collected