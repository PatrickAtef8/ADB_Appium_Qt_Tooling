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
        """Navigate directly to a profile using Instagram deep link."""
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
            result = _run_hidden(cmd, capture_output=True, text=True, timeout=10)
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
                self._log(f"⏳ Page still loading, retrying {mode} list... (attempt {attempt}, up to {MAX_WAIT}s)")
                time.sleep(RETRY_WAIT)

            for res_id in STAT_ID_MAP.get(mode_lower, STAT_FALLBACK_IDS):
                try:
                    el = driver.find_element(AppiumBy.ID, res_id)
                    self._log(f"✅ Found {mode} stat container by ID: '{el.text}'")
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
                        self._log(f"✅ Found {mode} stat by text matching: '{tv.text}'")
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
                self._log(f"✅ Found {mode} stat by description: '{el.get_attribute('content-desc')}'")
                el.click()
                time.sleep(2.5)
                return True
            except Exception:
                pass

        self._log(f"❌ Failed to open {mode} list after {MAX_WAIT}s")
        return False

    def _extract_visible_accounts(self) -> List[Dict]:
        """Extract usernames from the current screen."""
        driver = self.ctrl.driver
        accounts = []
        try:
            rows = driver.find_elements(AppiumBy.ID, IG_USER_ROW)
            for row in rows:
                try:
                    uname_el = row.find_element(AppiumBy.ID, "com.instagram.android:id/follow_list_username")
                    uname = uname_el.text.strip()
                    if uname:
                        fname = ""
                        try:
                            fname_el = row.find_element(AppiumBy.ID, "com.instagram.android:id/follow_list_subtitle")
                            fname = fname_el.text.strip()
                        except: pass
                        
                        accounts.append({
                            "username": uname,
                            "full_name": fname,
                        })
                except Exception:
                    continue
            
            if not accounts:
                all_tvs = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                for tv in all_tvs:
                    txt = tv.text.strip()
                    if txt and " " not in txt and len(txt) > 2 and len(txt) < 31:
                        if txt.lower() not in ("followers", "following", "posts", "search", "suggested"):
                            accounts.append({"username": txt, "full_name": ""})
        except Exception as e:
            self._log(f"Extraction error: {e}")
        return accounts

    def open_profile_details(self, username: str, filters: dict = None) -> dict:
        """Click on a username to open its profile and scrape full details."""
        driver = self.ctrl.driver
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

        try:
            try:
                el = driver.find_element(AppiumBy.ANDROID_UIAUTOMATOR, f'new UiSelector().text("{username}")')
                el.click()
            except:
                all_tvs = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                found = False
                for tv in all_tvs:
                    if tv.text.strip().lower() == username.lower():
                        tv.click()
                        found = True
                        break
                if not found: return details

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
                details["bio"] = driver.find_element(AppiumBy.ID, "com.instagram.android:id/profile_header_bio_text").text.strip()
            except: pass

            loc_ids = [
                "com.instagram.android:id/profile_header_location_text",
                "com.instagram.android:id/profile_header_business_category",
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
                profile_pic = driver.find_element(
                    AppiumBy.ID,
                    "com.instagram.android:id/profile_header_avatar_container_frame"
                )
                desc = (profile_pic.get_attribute("content-desc") or "").lower()
                uname_lower = username.lower()
                if uname_lower and uname_lower in desc:
                    details["has_profile_pic"] = True
                elif desc and desc not in ("profile photo", "profile picture", "photo"):
                    details["has_profile_pic"] = True
                else:
                    details["has_profile_pic"] = False
            except Exception:
                details["has_profile_pic"] = True

            if not details["is_private"] and details["post_count"] > 0:
                details["has_recent_post"] = True
                
                enable_spin = filters.get("enable_post_spin", False) if filters else False
                months_threshold = int(filters.get("skip_no_posts_last_n_months", 0)) if filters else 0
                
                if enable_spin and months_threshold > 0:
                    try:
                        self._log(f"🔍 Attempting to click latest post for @{username}...")
                        
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
                                            self._log(f"✅ Found latest post using {by}: {val}")
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
                                        self._log(f"✅ Found latest post via fallback content-desc: {desc}")
                                        break
                            except: pass
                            if post_el: break

                            if attempt_find < 2:
                                self._log(f"🔍 Post not found (attempt {attempt_find+1}), scrolling slightly...")
                                self.scroll_list(swipe_distance=0.2)
                                time.sleep(2.0)
                        
                        if post_el:
                            try:
                                post_el.click()
                            except:
                                # Final fallback: Coordinate-based click if element click fails
                                self._log("⚠️ Element click failed, trying coordinate-based click...")
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
                                    self._log(f"⚠️ Accidentally opened a story/highlight for @{username} — pressing back and skipping post-date check")
                            except Exception:
                                pass

                            if landed_in_story:
                                driver.back()
                                time.sleep(1.5)
                                self._log(f"⚠️ Could not find date text on post for @{username} (landed in story/highlight)")
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
                            
                            for attempt in range(4):
                                for d_id in date_ids:
                                    try:
                                        date_el = driver.find_element(AppiumBy.ID, d_id)
                                        txt = date_el.text.strip()
                                        if txt and re.search(date_regex, txt, re.I):
                                            date_text = txt
                                            break
                                    except: continue
                                if date_text: break
                                
                                all_tvs = driver.find_elements(AppiumBy.CLASS_NAME, "android.widget.TextView")
                                for tv in reversed(all_tvs):
                                    txt = tv.text.strip()
                                    if re.search(date_regex, txt, re.I):
                                        date_text = txt
                                        break
                                if date_text: break
                                
                                self._log(f"📜 Post date not visible (attempt {attempt+1}), scrolling and waiting...")
                                self.scroll_list(swipe_distance=0.4) # Uses ADB shell swipe now
                                time.sleep(2)
                            
                            if date_text:
                                self._log(f"✅ Latest post date for @{username}: {date_text}")
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
                                        self._log(f"⏭️ Skipping @{username}: Latest post is {age_months_exact:.1f} months old (threshold: >{months_threshold})")
                                        details["has_recent_post"] = False
                                    else:
                                        self._log(f"✅ @{username}: Post is {age_months_exact:.1f} months old — within threshold")
                                else:
                                    self._log(f"⚠️ Could not parse date '{date_text}', proceeding anyway...")
                            else:
                                self._log(f"⚠️ Could not find date text on post for @{username} after 4 attempts")
                            
                            self._log(f"⬅️ Pressing back to return to profile...")
                            driver.back()
                            time.sleep(1.5)
                            
                            if details.get("has_recent_post") == False:
                                self._log(f"⬅️ Returning early due to skip...")
                                return details
                        else:
                            self._log(f"❌ No posts found to click for @{username}")
                    except Exception as e:
                        self._log(f"⚠️ Could not check latest post for @{username}: {e}")

            details["country_code"] = infer_country_code(
                details["phone"], details["location"]
            )

        except Exception as e:
            self._log(f"Could not open profile of {username}: {e}")

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
            self._log("🏠 Pressed Back to clear screen before account switch")
            return True
        except Exception as e:
            self._log(f"⚠️ _appium_navigate_to_home error: {e}")
            return False

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
            self._log(f"⚠️ ADB scroll failed: {e}, trying Appium fallback...")
            try:
                driver = self.ctrl.driver
                size = driver.get_window_size()
                w, h = size["width"], size["height"]
                start_y = int(h * 0.75)
                end_y = int(h * (0.75 - swipe_distance))
                driver.swipe(w // 2, start_y, w // 2, end_y, 600)
            except Exception as e2:
                self._log(f"❌ All scroll methods failed: {e2}")

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
            self._log(f"❌ Failed to navigate to @{target_username}")
            return 0

        if not self.open_list(mode):
            self._log(f"❌ Failed to open {mode} list for @{target_username}")
            return 0

        self._log(f"✅ Opened {mode} list. Starting collection...")
        consecutive_empty = 0

        while collected < max_count and not self._stop_flag:
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
                continue

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
                if self._stop_flag or self._need_reopen_list:
                    break
                if collected >= max_count:
                    break

                uname = acc["username"].lower()
                # Check BOTH the current session's seen list AND the global blacklist
                if uname in seen_usernames or uname in blacklist:
                    # We don't log "Duplicate skipped" here to avoid cluttering the log
                    # unless it was a new discovery in the same session.
                    continue
                seen_usernames.add(uname)

                if fetch_details:
                    details = self.open_profile_details(acc["username"], filters=filters)
                    acc.update(details)
                    self.ctrl.press_back()
                    time.sleep(_rand(profile_delay_min, profile_delay_max))

                if should_skip(acc, filters, blacklist):
                    self._log(f"⏭️ Skipped (filtered): @{acc['username']}")
                    continue

                if self.on_account_found:
                    self.on_account_found(acc)

                add_to_blacklist(acc["username"])
                # Also add to local blacklist set to prevent re-processing in same loop
                blacklist.add(uname)
                
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

                if self.on_switch_check:
                    self.on_switch_check(collected)

            if not self._need_reopen_list:
                self.scroll_list()
                time.sleep(_rand(scroll_delay_min, scroll_delay_max))

        self._log(f"🏁 Done. Collected {collected} accounts.")
        return collected