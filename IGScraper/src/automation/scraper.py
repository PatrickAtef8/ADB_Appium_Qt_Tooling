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

from .appium_controller import AppiumController
from src.utils.filters import extract_email, extract_phone, infer_country_code, should_skip
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
        """Navigate directly to a profile using Instagram deep link."""
        import subprocess
        driver = self.ctrl.driver
        self._log(f"Navigating to profile: @{username}")

        # Strategy 1: ADB deep link
        try:
            serial = self.ctrl._device_serial or ""
            cmd = [
                "adb", "-s", serial, "shell", "am", "start",
                "-a", "android.intent.action.VIEW",
                "-d", f"https://www.instagram.com/{username}/",
                "com.instagram.android"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if "Error" not in result.stderr and "error" not in result.stdout.lower():
                self._log(f"✅ Opened profile via deep link: @{username}")
                time.sleep(4)
                try:
                    driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiSelector().textContains("follower")'
                    )
                    self._log("✅ Profile page confirmed loaded")
                    return True
                except Exception:
                    self._log("⚠️ Deep link opened but profile not confirmed, continuing...")
                    return True
        except Exception as e:
            self._log(f"Deep link failed: {e}, trying search fallback...")

        # Strategy 2: Search
        self._log("Trying search method...")
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
                self._log("❌ Could not find search tab")
                return False

        try:
            search_box = self._find(AppiumBy.ANDROID_UIAUTOMATOR,
                'new UiSelector().className("android.widget.EditText")', timeout=8)
            search_box.click()
            time.sleep(0.5)
            search_box.clear()
            search_box.send_keys(username)
            time.sleep(3)
        except Exception as e:
            self._log(f"Search box error: {e}")
            return False

        try:
            time.sleep(1)
            all_texts = self._find_all(AppiumBy.XPATH, '//android.widget.TextView')
            found_accounts = False
            for el in all_texts:
                try:
                    txt = el.text
                    if txt == "Accounts":
                        found_accounts = True
                        continue
                    if found_accounts and txt.lower() == username.lower():
                        el.click()
                        time.sleep(4)
                        return True
                except Exception:
                    continue

            results = self._find_all(AppiumBy.XPATH,
                f'//android.widget.TextView[@text="{username}"]')
            if results:
                results[0].click()
                time.sleep(4)
                return True
        except Exception as e:
            self._log(f"Could not navigate to profile: {e}")

        return False

    def open_list(self, mode: str) -> bool:
        """Open followers or following list."""
        driver = self.ctrl.driver
        self._log(f"Opening {mode} list...")
        time.sleep(2)

        try:
            el = driver.find_element(
                AppiumBy.ANDROID_UIAUTOMATOR,
                f'new UiSelector().textContains("{mode}").clickable(true)'
            )
            self._log(f"✅ Found {mode} button: '{el.text}'")
            el.click()
            time.sleep(2.5)
            return True
        except Exception:
            pass

        try:
            elements = self._find_all(
                AppiumBy.XPATH,
                f'//android.widget.TextView[contains(translate(@text,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{mode.lower()}")]'
            )
            for el in elements:
                if mode.lower() in el.text.lower():
                    el.click()
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

        self._log(f"❌ Could not find {mode} button.")
        return False

    # ── Row extraction ────────────────────────────────────────────────────────

    def _extract_visible_accounts(self) -> List[Dict]:
        """Extract all visible account rows on screen including story detection."""
        driver = self.ctrl.driver
        accounts = []

        row_ids = [
            "com.instagram.android:id/follow_list_container",
            "com.instagram.android:id/row_user_container_base",
            "com.instagram.android:id/unified_follow_list_user_container",
        ]

        rows = []
        for rid in row_ids:
            rows = self._find_all(AppiumBy.ID, rid)
            if rows:
                break

        if not rows:
            rows = self._find_all(
                AppiumBy.XPATH,
                '//androidx.recyclerview.widget.RecyclerView/android.widget.LinearLayout'
            )

        for row in rows:
            try:
                acc = self._parse_row(row)
                if acc and acc.get("username"):
                    accounts.append(acc)
            except (StaleElementReferenceException, Exception):
                continue

        return accounts

    def _parse_row(self, row_element) -> Optional[Dict]:
        """Parse username, full name, profile pic presence, and story ring from a row."""
        username = ""
        full_name = ""
        has_profile_pic = True
        has_story = False

        try:
            u_el = row_element.find_element(
                AppiumBy.ID, "com.instagram.android:id/follow_list_username"
            )
            username = u_el.text.strip()
        except Exception:
            try:
                texts = row_element.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                if texts:
                    username = texts[0].text.strip()
                if len(texts) > 1:
                    full_name = texts[1].text.strip()
            except Exception:
                pass

        for fn_id in [
            "com.instagram.android:id/follow_list_subtitle",
            "com.instagram.android:id/follow_list_full_name",
        ]:
            try:
                fn_el = row_element.find_element(AppiumBy.ID, fn_id)
                val = fn_el.text.strip()
                if val:
                    full_name = val
                    break
            except Exception:
                continue

        # Detect story ring (colored ring around avatar = active story)
        try:
            story_indicators = row_element.find_elements(
                AppiumBy.ANDROID_UIAUTOMATOR,
                'new UiSelector().descriptionContains("story")'
            )
            if story_indicators:
                has_story = True
            avatar_containers = row_element.find_elements(
                AppiumBy.CLASS_NAME, "android.widget.FrameLayout"
            )
            for container in avatar_containers:
                try:
                    desc = container.get_attribute("content-desc") or ""
                    if "story" in desc.lower():
                        has_story = True
                        break
                except Exception:
                    pass
        except Exception:
            pass

        # Detect if profile picture is present
        try:
            img_views = row_element.find_elements(
                AppiumBy.CLASS_NAME, "android.widget.ImageView"
            )
            if not img_views:
                has_profile_pic = False
        except Exception:
            pass

        if not username:
            return None

        return {
            "username": username,
            "full_name": full_name,
            "bio": "",
            "email": "",
            "phone": "",
            "location": "",
            "country_code": "",
            "followers": 0,
            "following": 0,
            "post_count": 0,
            "has_profile_pic": has_profile_pic,
            "has_story": has_story,
            "has_recent_post": True,
            "is_private": False,
            "profile_url": f"https://www.instagram.com/{username}/",
            "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ── Profile detail extraction ─────────────────────────────────────────────

    def open_profile_details(self, username: str) -> Dict:
        """
        Tap a user in the list to open their profile and get full details.
        Returns dict with bio, followers, following, post_count, email, phone,
        location, country_code, has_recent_post, is_private, has_profile_pic.
        """
        driver = self.ctrl.driver
        details = {
            "bio": "", "email": "", "phone": "",
            "location": "", "country_code": "",
            "followers": 0, "following": 0, "post_count": 0,
            "is_private": False, "has_profile_pic": True,
            "has_recent_post": True,
        }

        try:
            el = driver.find_element(
                AppiumBy.XPATH,
                f'//android.widget.TextView[@text="{username}"]'
            )
            el.click()
            time.sleep(3)

            # Full name
            try:
                fn_el = driver.find_element(
                    AppiumBy.ID,
                    "com.instagram.android:id/profile_header_full_name_above_vanity"
                )
                details["full_name"] = fn_el.text.strip()
            except Exception:
                pass

            # Bio
            bio_text = ""
            for bio_id in [
                "com.instagram.android:id/profile_user_info_compose_view",
                "com.instagram.android:id/profile_header_bio_text",
                "com.instagram.android:id/text_view",
            ]:
                try:
                    bio_el = driver.find_element(AppiumBy.ID, bio_id)
                    txt = bio_el.text.strip()
                    if txt and not txt.replace(",", "").replace(".", "").isdigit() \
                            and txt.lower() not in ("posts", "followers", "following", "follow", "message"):
                        bio_text = txt
                        break
                except Exception:
                    continue
            details["bio"] = bio_text

            # Extract email and phone from bio
            if bio_text:
                details["email"] = extract_email(bio_text)
                details["phone"] = extract_phone(bio_text)

            # Location field
            for loc_id in [
                "com.instagram.android:id/profile_header_location_container",
                "com.instagram.android:id/profile_header_location",
            ]:
                try:
                    loc_el = driver.find_element(AppiumBy.ID, loc_id)
                    loc_txt = loc_el.text.strip()
                    if loc_txt:
                        details["location"] = loc_txt
                        break
                except Exception:
                    continue

            # Try to get contact info from the "Contact" button
            try:
                contact_btn = driver.find_element(
                    AppiumBy.ANDROID_UIAUTOMATOR,
                    'new UiSelector().textContains("Contact").clickable(true)'
                )
                contact_btn.click()
                time.sleep(2)

                try:
                    all_text_els = driver.find_elements(
                        AppiumBy.CLASS_NAME, "android.widget.TextView"
                    )
                    for el in all_text_els:
                        txt = el.text.strip()
                        if txt and not details["email"]:
                            found_email = extract_email(txt)
                            if found_email:
                                details["email"] = found_email
                        if txt and not details["phone"]:
                            if re.search(r"\+?\d[\d\s\-\(\)]{6,}", txt):
                                details["phone"] = extract_phone(txt)
                except Exception:
                    pass

                driver.back()
                time.sleep(1)
            except Exception:
                pass

            # Also try "Email" link button directly
            if not details["email"]:
                try:
                    email_btn = driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiSelector().textContains("Email").clickable(true)'
                    )
                    email_text = email_btn.get_attribute("content-desc") or email_btn.text
                    found = extract_email(email_text)
                    if found:
                        details["email"] = found
                except Exception:
                    pass

            # Followers & Following counts
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

            # Try combined "54.3K followers" style text
            if not details["followers"]:
                try:
                    el = driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiSelector().textContains("followers")'
                    )
                    num = el.text.lower().replace("followers", "").strip()
                    details["followers"] = _parse_count(num)
                except Exception:
                    pass

            # Post count from posts header
            if not details["post_count"]:
                try:
                    el = driver.find_element(
                        AppiumBy.ANDROID_UIAUTOMATOR,
                        'new UiSelector().textContains("posts")'
                    )
                    num = el.text.lower().replace("posts", "").strip()
                    details["post_count"] = _parse_count(num)
                except Exception:
                    pass

            # Private account?
            try:
                driver.find_element(
                    AppiumBy.XPATH,
                    '//*[contains(@text,"This account is private") or contains(@content-desc,"private")]'
                )
                details["is_private"] = True
            except Exception:
                details["is_private"] = False

            # Has profile picture?
            try:
                profile_pic = driver.find_element(
                    AppiumBy.ID,
                    "com.instagram.android:id/profile_header_avatar_container_frame"
                )
                desc = profile_pic.get_attribute("content-desc") or ""
                details["has_profile_pic"] = len(desc) > 5
            except Exception:
                details["has_profile_pic"] = True

            if not details["is_private"] and details["post_count"] > 0:
                details["has_recent_post"] = True

            # Country code inference
            details["country_code"] = infer_country_code(
                details["phone"], details["location"]
            )

        except Exception as e:
            self._log(f"Could not open profile of {username}: {e}")

        return details

    def _appium_navigate_to_home(self) -> bool:
        """
        Use the live Appium session to dismiss the following list, then use
        ADB 'am start --activity-clear-top' to collapse the Instagram back
        stack to a single root activity BEFORE releasing the session for
        ADB-based account switching.

        Why --activity-clear-top is critical
        -------------------------------------
        navigate_to_profile() uses 'am start -a android.intent.action.VIEW'
        (a deep link) which PUSHES a new Activity onto Instagram's back stack.
        After the first switch the re-navigation does this deep link again, so
        by the time the second switch fires the stack looks like:

            HomeActivity → DeepLinkProfileActivity → FollowingListSheet

        Without clearing the stack, the Back presses inside
        switch_instagram_account() Phase B must traverse ALL those layers.
        If the loop runs out of attempts while still inside the deep-link
        Profile activity, the next Back press exits Instagram to the launcher
        (Instagram leaves the foreground).

        --activity-clear-top pops every Activity above the main one in a
        single command, leaving exactly:

            InstagramMainActivity   (depth = 1, always safe to Back from)

        This makes the stack depth identical whether it is the 1st or the
        50th switch, so Phase B always finds the chevron on the very first
        Profile-tab tap.
        """
        import subprocess

        driver = self.ctrl.driver
        serial = self.ctrl._device_serial or ""

        try:
            # Step 1: Dismiss the following list via Appium back() (max 6 presses)
            # We check for all known list container IDs so we catch every variant.
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

            # Step 2: Use ADB to collapse the entire Instagram back stack to
            # the main activity in one shot.  This works even while the
            # Appium session is still connected because am-start does not
            # conflict with UiAutomator2 — it only modifies the activity
            # manager task stack.
            subprocess.run(
                [
                    "adb", "-s", serial, "shell", "am", "start",
                    "--activity-clear-top",
                    "-n", "com.instagram.android/"
                         "com.instagram.mainactivity.InstagramMainActivity",
                ],
                capture_output=True, text=True, timeout=10,
            )
            time.sleep(2.0)   # let the animation finish

            self._log("🏠 Pressed Back to clear screen before account switch")
            return True

        except Exception as e:
            self._log(f"⚠️ _appium_navigate_to_home error (non-fatal): {e}")
            # Even on error the ADB switch can still attempt — not fatal.
            return False

    def scroll_list(self, swipe_distance: float = 0.6):
        """Scroll the followers/following list down."""
        driver = self.ctrl.driver
        size = driver.get_window_size()
        w, h = size["width"], size["height"]
        start_y = int(h * 0.75)
        end_y = int(h * (0.75 - swipe_distance))
        driver.swipe(w // 2, start_y, w // 2, end_y, 600)

    # ── Main loop ─────────────────────────────────────────────────────────────

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
        """
        Main scraping loop.
        Returns number of accounts collected.
        """
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
        break_every       = int(delays.get("session_break_every",    100))
        break_duration    = int(delays.get("session_break_duration",  30))

        if not self.navigate_to_profile(target_username):
            self._log(f"❌ Failed to navigate to @{target_username}")
            return 0

        if not self.open_list(mode):
            self._log(f"❌ Failed to open {mode} list for @{target_username}")
            return 0

        self._log(f"✅ Opened {mode} list. Starting collection...")
        consecutive_empty = 0

        while collected < max_count and not self._stop_flag:

            # ── Re-navigate after account switch ──────────────────────────
            if self._need_reopen_list:
                self._need_reopen_list = False
                self._log(f"🔄 Re-opening {mode} list after account switch…")
                if not self.navigate_to_profile(target_username):
                    self._log("❌ Re-navigation failed after switch, stopping.")
                    break
                if not self.open_list(mode):
                    self._log("❌ Could not reopen list after switch, stopping.")
                    break
                consecutive_empty = 0
                continue   # restart while-loop on fresh list screen
            # ─────────────────────────────────────────────────────────────

            accounts = self._extract_visible_accounts()

            if not accounts:
                consecutive_empty += 1
                if consecutive_empty >= 5:
                    self._log("⚠️ No more accounts found. Reached end of list.")
                    break
                self.scroll_list()
                time.sleep(_rand(scroll_delay_min, scroll_delay_max))
                continue

            consecutive_empty = 0

            for acc in accounts:
                # Break inner loop immediately if stop or switch was requested
                if self._stop_flag or self._need_reopen_list:
                    break
                if collected >= max_count:
                    break

                uname = acc["username"].lower()
                if uname in seen_usernames or uname in blacklist:
                    continue
                seen_usernames.add(uname)

                # Fetch full profile details
                if fetch_details:
                    details = self.open_profile_details(acc["username"])
                    acc.update(details)
                    self.ctrl.press_back()
                    time.sleep(_rand(profile_delay_min, profile_delay_max))

                # Apply all filters
                if should_skip(acc, filters, blacklist):
                    self._log(f"⏭️ Skipped (filtered): @{acc['username']}")
                    continue

                # Emit account
                if self.on_account_found:
                    self.on_account_found(acc)

                # Add to blacklist so it's never scraped again
                add_to_blacklist(acc["username"])

                collected += 1

                if self.on_progress:
                    self.on_progress(collected, max_count)

                self._log(
                    f"[{collected}/{max_count}] ✔ @{acc['username']} | "
                    f"email={acc.get('email', '-')} | "
                    f"phone={acc.get('phone', '-')} | "
                    f"country={acc.get('country_code', '-')} | "
                    f"posts={acc.get('post_count', '-')}"
                )

                # ── Account switch check ───────────────────────────────────
                # Called after every collected profile. If the threshold is
                # reached, on_switch_check sets _need_reopen_list = True so
                # the outer while-loop re-navigates before the next profile.
                if self.on_switch_check:
                    self.on_switch_check(collected)
                # ─────────────────────────────────────────────────────────

                # Session break (pause only — no switch logic here).
                # Skip if a switch was just triggered on this same tick:
                # the switch already pauses for several seconds internally,
                # and _need_reopen_list being True means we are about to
                # re-navigate — adding another break here is redundant and
                # would delay the re-navigation unnecessarily.
                if (break_every > 0
                        and collected % break_every == 0
                        and not self._need_reopen_list):
                    self._log(f"⏸️ Session break for {break_duration}s...")
                    time.sleep(break_duration)

            # Scroll to reveal more rows (outer loop continues)
            if not self._need_reopen_list:
                self.scroll_list()
                time.sleep(_rand(scroll_delay_min, scroll_delay_max))

        self._log(f"🏁 Done. Collected {collected} accounts.")
        return collected