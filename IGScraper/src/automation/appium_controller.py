"""
Appium controller: manages Appium sessions, Android device connections,
and account switching on Instagram.
"""

import os
import shutil
import subprocess
import time
import re
from typing import List, Optional, Tuple
from appium import webdriver
from appium.options.android import UiAutomator2Options

INSTAGRAM_PACKAGE = "com.instagram.android"
SCRCPY_PATH = "/usr/local/bin/scrcpy"


def _get_instagram_activity(serial: str) -> str:
    """Always return main activity for stability on Android 7 + old Instagram."""
    return "com.instagram.mainactivity.InstagramMainActivity"


def get_connected_devices() -> List[Tuple[str, str]]:
    """Returns list of (serial, model_name) for all connected ADB devices."""
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.strip().splitlines()
        devices = []
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == "device":
                serial = parts[0]
                name_result = subprocess.run(
                    ["adb", "-s", serial, "shell", "getprop", "ro.product.model"],
                    capture_output=True, text=True, timeout=5
                )
                model = name_result.stdout.strip() or serial
                devices.append((serial, model))
        return devices
    except Exception:
        return []


def get_instagram_accounts(serial: str) -> List[str]:
    """
    Detect all logged-in Instagram accounts on the device.

    Opens Instagram if not already running (monkey launch), then navigates
    to the profile tab via Back-key navigation (no restart), opens the
    account switcher, and reads all account names.
    """
    accounts = []
    try:
        # Make sure Instagram is open
        top = subprocess.run(
            ["adb", "-s", serial, "shell", "dumpsys", "activity", "top"],
            capture_output=True, text=True, timeout=10
        )
        if INSTAGRAM_PACKAGE not in top.stdout:
            subprocess.run(
                ["adb", "-s", serial, "shell",
                 "monkey", "-p", INSTAGRAM_PACKAGE,
                 "-c", "android.intent.category.LAUNCHER", "1"],
                capture_output=True, text=True, timeout=10
            )
            for _ in range(10):
                time.sleep(1)
                check = subprocess.run(
                    ["adb", "-s", serial, "shell", "dumpsys", "activity", "top"],
                    capture_output=True, text=True, timeout=10
                )
                if INSTAGRAM_PACKAGE in check.stdout:
                    break
            time.sleep(3)
        else:
            time.sleep(1)

        # Navigate to profile tab via Back presses
        _navigate_to_profile_tab_via_back(serial)

        # Open account switcher
        switcher_xml = _go_to_profile_and_open_switcher(serial)
        if not switcher_xml:
            return ["Account 1"]

        account_rows = _parse_account_rows(switcher_xml)
        accounts = [name for name, _ in account_rows]

        # Close the switcher
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
            capture_output=True, text=True, timeout=5
        )
        time.sleep(1)

    except Exception:
        pass

    return accounts if accounts else ["Account 1"]


def _dump_ui(serial: str) -> str:
    """Dump the current UI hierarchy and return raw XML."""
    subprocess.run(
        ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/_ig_ui.xml"],
        capture_output=True, text=True, timeout=10
    )
    return subprocess.run(
        ["adb", "-s", serial, "shell", "cat", "/sdcard/_ig_ui.xml"],
        capture_output=True, text=True, timeout=10
    ).stdout


def _tap_bounds(serial: str, x1: int, y1: int, x2: int, y2: int):
    """Tap the centre of a bounding box."""
    x = (x1 + x2) // 2
    y = (y1 + y2) // 2
    subprocess.run(
        ["adb", "-s", serial, "shell", "input", "tap", str(x), str(y)],
        capture_output=True, text=True, timeout=5
    )


def _find_bounds(xml: str, pattern: str):
    """
    Search xml for pattern and return (x1,y1,x2,y2) from the first
    capture group which must be a bounds string '[x1,y1][x2,y2]'.
    Returns None if not found.
    """
    m = re.search(pattern, xml)
    if not m:
        return None
    bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", m.group(1))
    if not bm:
        return None
    return int(bm.group(1)), int(bm.group(2)), int(bm.group(3)), int(bm.group(4))


def _get_screen_height(xml: str) -> int:
    """Extract screen height from the root node bounds. Falls back to 1920."""
    root_m = re.search(r'bounds="\[0,0\]\[(\d+),(\d+)\]"', xml)
    if root_m:
        return int(root_m.group(2))
    return 1920


def _find_nav_profile_tab(xml: str):
    """
    Find the bottom navigation bar Profile tab — NOT suggestion cards,
    tagged-photo icons, or any other person-shaped element in page content.

    Root cause of the original bug
    --------------------------------
    On the Home feed with 'Follow' suggestion cards, those cards contain a
    person icon that can carry content-desc="Profile".  The old code picked
    the element with the highest y1 value, but suggestion cards at ~y=820
    beat out the nav bar at ~y=900 only on some builds/screens, or landed
    close enough that the tap hit the card.

    Fix: strict Y-floor filter
    --------------------------
    The nav bar sits in the bottom ~15 % of the screen (y1 >= 78 % of
    screen height).  Any element above that threshold is page content and
    is unconditionally rejected.  This makes it impossible for a suggestion
    card or feed icon to be returned regardless of screen resolution.
    """
    screen_h = _get_screen_height(xml)
    nav_floor_y = int(screen_h * 0.78)   # nav bar always below this line

    # Strategy 1: unambiguous resource-id, validated to be on-screen
    for m in re.finditer(
        r'resource-id="com\.instagram\.android:id/profile_tab"'
        r'[^>]*bounds="(\[\d+,\d+\]\[\d+,\d+\])"',
        xml
    ):
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", m.group(1))
        if bm:
            x1, y1, x2, y2 = int(bm.group(1)), int(bm.group(2)), int(bm.group(3)), int(bm.group(4))
            if y1 >= nav_floor_y:
                return x1, y1, x2, y2
        # resource-id found but above the floor → off-screen cached node, skip

    # Strategy 2: content-desc="Profile" with mandatory Y-floor
    candidates = []
    for m in re.finditer(
        r'content-desc="Profile"[^>]*bounds="(\[\d+,\d+\]\[\d+,\d+\])"',
        xml
    ):
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", m.group(1))
        if bm:
            x1, y1, x2, y2 = int(bm.group(1)), int(bm.group(2)), int(bm.group(3)), int(bm.group(4))
            if y1 >= nav_floor_y:          # only nav-bar elements pass
                candidates.append((x1, y1, x2, y2))

    if candidates:
        return max(candidates, key=lambda b: b[1])

    return None


def _go_to_profile_and_open_switcher(serial: str) -> str:
    """
    Navigate to the Instagram profile tab and open the account switcher.

    Retries the full sequence up to 3 times:
      1. Tap the profile tab (found by content-desc or resource-id in live XML)
      2. Wait for the chevron to appear
      3. Tap the chevron
      4. Wait for row_user_textview to appear in the dump

    Returns the switcher XML on success, empty string on failure.
    """
    for attempt in range(3):
        # ── Step 1: tap profile tab ───────────────────────────────────────
        xml = _dump_ui(serial)

        bounds = _find_nav_profile_tab(xml)

        if bounds:
            _tap_bounds(serial, *bounds)
            time.sleep(2)

        # ── Step 2: wait for chevron (poll up to 5s) ──────────────────────
        chevron_bounds = None
        for _ in range(5):
            xml = _dump_ui(serial)
            chevron_bounds = _find_bounds(xml,
                r'resource-id="com\.instagram\.android:id/action_bar_title_chevron"'
                r'[^>]*bounds="(\[\d+,\d+\]\[\d+,\d+\])"'
            )
            if chevron_bounds:
                break
            time.sleep(1)

        if not chevron_bounds:
            time.sleep(2)
            continue  # retry full sequence

        # ── Step 3: tap chevron ───────────────────────────────────────────
        _tap_bounds(serial, *chevron_bounds)
        time.sleep(2)

        # ── Step 4: wait for switcher rows (poll up to 5s) ────────────────
        for _ in range(5):
            switcher_xml = _dump_ui(serial)
            if "row_user_textview" in switcher_xml:
                return switcher_xml
            time.sleep(1)

        # Switcher didn't appear — close whatever opened and retry
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
            capture_output=True, text=True, timeout=5
        )
        time.sleep(2)

    return ""


def _parse_account_rows(xml: str) -> List[Tuple[str, Tuple[int,int,int,int]]]:
    """
    Parse the account switcher XML and return (username, tap_bounds) for
    every account row except 'Add account'.

    Tap-target strategy
    -------------------
    Instagram's switcher has two tap zones per row:

      * Avatar  (row_user_imageview)  -- opens profile preview; does NOT
        reliably switch the account in all builds.

      * Switch button (row_user_button / row_user_switch_button or a generic
        android.widget.Button on the right side) -- this is the element that
        actually performs the account switch for inactive rows.  It is absent
        on the currently-active account row (Instagram shows a checkmark there
        instead, with no tappable switch button).

    We prefer the switch button when present and fall back to the avatar only
    when no button is found.  Callers must skip the row matching the current
    account name so we never attempt to tap the active row at all.
    """
    SKIP = {"add account"}

    segments = re.split(
        r'(?=resource-id="com\.instagram\.android:id/row_user_imageview")',
        xml
    )

    results: List[Tuple[str, Tuple[int,int,int,int]]] = []

    for seg in segments:
        # ── Avatar bounds (fallback tap target) ──────────────────────────
        img_m = re.search(
            r'resource-id="com\.instagram\.android:id/row_user_imageview"'            r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            seg
        )
        if not img_m:
            continue
        avatar_bounds = (
            int(img_m.group(1)), int(img_m.group(2)),
            int(img_m.group(3)), int(img_m.group(4))
        )

        # ── Username ─────────────────────────────────────────────────────
        name_m = (
            re.search(
                r'text="([^"]+)"[^>]*'                r'resource-id="com\.instagram\.android:id/row_user_textview"',
                seg
            ) or
            re.search(
                r'resource-id="com\.instagram\.android:id/row_user_textview"'                r'[^>]*text="([^"]+)"',
                seg
            )
        )
        if not name_m:
            continue
        name = name_m.group(1).strip()
        if not name or name.lower() in SKIP:
            continue

        # ── Switch button bounds (preferred tap target for inactive rows) ─
        # Try known resource-ids first, then fall back to any Button widget
        # in the segment (the button sits on the right side of every inactive
        # row and is the only element that actually triggers the account switch).
        btn_bounds = None
        for btn_pat in [
            r'resource-id="com\.instagram\.android:id/row_user_button"'            r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            r'resource-id="com\.instagram\.android:id/row_user_switch_button"'            r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            r'class="android\.widget\.Button"[^>]*'            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        ]:
            bm = re.search(btn_pat, seg)
            if bm:
                btn_bounds = (int(bm.group(1)), int(bm.group(2)),
                              int(bm.group(3)), int(bm.group(4)))
                break

        tap_bounds = btn_bounds if btn_bounds else avatar_bounds

        if name not in [r[0] for r in results]:
            results.append((name, tap_bounds))

    return results


def _navigate_to_profile_tab_via_back(serial: str) -> bool:
    """
    Navigate to the profile tab WITHOUT restarting Instagram.

    Presses the Android Back key up to 5 times until the bottom navigation
    bar (which contains the Profile tab) becomes visible in the UI dump.
    Then taps the Profile tab.

    This is far more stable than restarting Instagram because:
    - No cold start / app restart that can crash Instagram
    - No Appium session conflict
    - Works from any screen inside the app (followers list, profile, feed, etc.)
    """
    for _ in range(6):  # max 5 back presses + 1 final check
        xml = _dump_ui(serial)

        # Check if bottom nav bar is visible — profile tab will be there
        profile_bounds = _find_nav_profile_tab(xml)

        if profile_bounds:
            _tap_bounds(serial, *profile_bounds)
            time.sleep(2)
            return True

        # Nav bar not visible — press Back to go up one level
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
            capture_output=True, text=True, timeout=5
        )
        time.sleep(1.5)

    return False


def _parse_switcher_rows(xml: str):
    """
    Parse the account-switcher bottom sheet and return
    (username, tap_x, tap_y) for every row except 'Add account'.

    TAP TARGET: the center of the full row LinearLayout — NOT the avatar.
    Tapping the avatar opens a profile preview; tapping anywhere else in
    the row triggers the actual account switch.

    Row structure in this Instagram build:
      <LinearLayout bounds="[0,Y1][1080,Y2]">           ← full row
        <FrameLayout>
          <ImageView id/row_user_imageview .../>          ← avatar (DO NOT TAP)
        </FrameLayout>
        <LinearLayout>
          <TextView id/row_user_textview text="username"/> ← username label
        </LinearLayout>
        <ImageView id/check .../>                         ← checkmark (active) or switch btn
      </LinearLayout>

    We extract:
      username  from row_user_textview
      row_y1    from the parent LinearLayout bounds
      row_y2    from the parent LinearLayout bounds
      tap_x = 600  (right of avatar, on the username text area — always safe)
      tap_y = (row_y1 + row_y2) // 2
    """
    import re as _re
    SKIP = {"add account"}
    results = []

    # Split on each row_user_imageview occurrence to get one segment per row
    segments = _re.split(
        r'(?=resource-id="com\.instagram\.android:id/row_user_imageview")',
        xml
    )

    for seg in segments:
        # Must have a username
        name_m = (
            _re.search(
                r'resource-id="com\.instagram\.android:id/row_user_textview"'
                r'[^>]*text="([^"]+)"',
                seg
            ) or
            _re.search(
                r'text="([^"]+)"[^>]*'
                r'resource-id="com\.instagram\.android:id/row_user_textview"',
                seg
            )
        )
        if not name_m:
            continue
        name = name_m.group(1).strip()
        if not name or name.lower() in SKIP:
            continue

        # Find the avatar bounds to know the row's Y range
        # The avatar sits inside the row, so its Y gives us the row Y
        avatar_m = _re.search(
            r'resource-id="com\.instagram\.android:id/row_user_imageview"'
            r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            seg
        )
        if not avatar_m:
            continue

        av_y1 = int(avatar_m.group(2))
        av_y2 = int(avatar_m.group(4))

        # Tap target: x=600 (well to the right of the avatar, on username text),
        # y = center of avatar row (avatar spans the full row height)
        tap_x = 600
        tap_y = (av_y1 + av_y2) // 2

        if name not in [r[0] for r in results]:
            results.append((name, tap_x, tap_y))

    return results


def switch_instagram_account(
    serial: str,
    account_name: str,
    current_account: Optional[str] = None,
) -> bool:
    """
    Switch Instagram to the given account WITHOUT force-stopping the app.

    ROOT CAUSE (confirmed from XML analysis):
    -----------------------------------------
    The following list is a bottom-sheet over the profile page. The nav bar
    IS in the XML but its coordinates are covered by list rows. Any tap while
    the list is visible hits a list row instead.

    Additionally, the old code tapped the chevron even when the switcher was
    ALREADY open (from a previous tap), which closed it again. And the old
    _parse_account_rows used avatar tap coords — tapping the avatar of an
    inactive account opens a profile preview, NOT a switch.

    This implementation fixes all three issues:

    Step 0  Press Back until the following list is gone from XML.
            Only tap the Profile tab when nav bar is visible AND no list present.
            Stop as soon as the chevron appears (own profile page confirmed).

    Step 1  Check if switcher is ALREADY open (row_user_textview in XML).
            If yes → skip to Step 3.  If no → tap chevron once.

    Step 2  Wait for switcher rows to appear.

    Step 3  Tap the TARGET ROW at x=600, y=row_center.
            x=600 is to the right of the avatar (x≈42-189), landing on the
            username text area. This is what triggers the actual switch.
            Tapping the avatar opens a profile preview instead.

    Step 4  Wait and verify Instagram is still running.
    """
    def _log(msg):
        print(f"[switch] {msg}", flush=True)

    try:
        _log(f"START: {current_account} -> {account_name}")

        # ── Step 0: dismiss list, reach own profile page ───────────────────
        #
        # CRITICAL: We do this in two strict phases to avoid phantom taps.
        #
        # Phase A — Press Back until the following/followers list is GONE.
        #           We do NOT tap anything during this phase. The nav bar is
        #           always present in the XML hierarchy even when it is hidden
        #           behind the list bottom-sheet, so checking has_nav is
        #           meaningless while the list is still visible. Any tap during
        #           Phase A would hit a list row, not the Profile tab.
        #
        # Phase B — Only after the list is confirmed absent (and a settle wait
        #           has passed), look for the chevron or the Profile tab and tap.
        _log("Step 0: Phase A — dismissing list sheet with Back presses...")
        step0_ok = False

        LIST_IDS = (
            'id/follow_list_container',
            'id/row_user_container_base',
            'id/unified_follow_list_user_container',
            'id/follow_list_username',
            'id/followers_list_container',
        )

        def _has_list(x: str) -> bool:
            return any(lid in x for lid in LIST_IDS)

        # Phase A: press Back until the list is gone (max 10 presses)
        for attempt in range(10):
            xml = _dump_ui(serial)
            if not _has_list(xml):
                # Double-check: wait for animation to settle then dump again.
                # A single dump can catch a frame mid-dismiss where the list
                # node is still in the XML but the sheet is visually gone.
                time.sleep(0.8)
                xml2 = _dump_ui(serial)
                if not _has_list(xml2):
                    _log(f"  Phase A done after {attempt} back-press(es) — list confirmed gone")
                    break
                # List reappeared in the second dump — treat as still present
                _log(f"  Phase A [{attempt}] list found in re-check — pressing Back")
                subprocess.run(
                    ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                    capture_output=True, text=True, timeout=5
                )
                time.sleep(2.0)
                continue
            _log(f"  Phase A [{attempt}] list still present — pressing Back")
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                capture_output=True, text=True, timeout=5
            )
            # Longer settle wait: Instagram list-dismiss animation takes ~400 ms
            # and the next uiautomator dump must see the POST-animation state.
            time.sleep(2.0)
        else:
            # After 10 back presses the list is still there — bail out
            _log("Step 0 Phase A FAILED: list never dismissed")
            return False

        # Extra settle: give Instagram time to finish rendering the destination
        # screen before we dump again and tap anything.
        time.sleep(1.5)

        # Phase B: now safely navigate to own profile page (no list on screen)
        _log("Step 0: Phase B — navigating to own profile page...")
        for attempt in range(8):
            xml = _dump_ui(serial)

            # Safety: if the list somehow reappeared, go back to Phase A logic
            if _has_list(xml):
                _log(f"  Phase B [{attempt}] list reappeared — pressing Back again")
                subprocess.run(
                    ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                    capture_output=True, text=True, timeout=5
                )
                time.sleep(2.0)
                continue

            has_chevron = 'id/action_bar_title_chevron' in xml

            if has_chevron:
                _log("  Phase B -> chevron visible. Own profile confirmed.")
                step0_ok = True
                break

            # Chevron not here yet — look for Profile tab in the nav bar
            profile_tab = _find_nav_profile_tab(xml)

            if profile_tab:
                _log(f"  Phase B [{attempt}] -> tapping Profile tab at {profile_tab}")
                _tap_bounds(serial, *profile_tab)
                # Wait for profile page + chevron to render
                time.sleep(3.0)
                xml2 = _dump_ui(serial)
                if 'id/action_bar_title_chevron' in xml2 and not _has_list(xml2):
                    _log("  Phase B -> chevron confirmed after Profile tab tap")
                    step0_ok = True
                    break
                # Chevron not there yet — loop again (don't tap Profile tab twice)
                time.sleep(1.0)
                continue

            # ── Foreground guard ───────────────────────────────────────────
            # Before pressing Back, confirm Instagram is still in the foreground.
            # If it is not (e.g. deep-link activity stack was over-popped and
            # the launcher is now on top), re-launch the main activity with
            # --activity-clear-top instead of pressing Back into the void.
            top_check = subprocess.run(
                ["adb", "-s", serial, "shell", "dumpsys", "activity", "top"],
                capture_output=True, text=True, timeout=10
            ).stdout
            if INSTAGRAM_PACKAGE not in top_check:
                _log(f"  Phase B [{attempt}] Instagram left foreground — re-launching main activity")
                subprocess.run(
                    [
                        "adb", "-s", serial, "shell", "am", "start",
                        "--activity-clear-top",
                        "-n", f"{INSTAGRAM_PACKAGE}/com.instagram.mainactivity.InstagramMainActivity",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                time.sleep(4.0)
                continue

            # Neither chevron nor Profile tab visible — we may be on a sub-screen
            _log(f"  Phase B [{attempt}] -> no chevron or Profile tab — pressing Back")
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                capture_output=True, text=True, timeout=5
            )
            time.sleep(2.0)

        if not step0_ok:
            _log("Step 0 FAILED")
            return False

        # ── Step 1: open switcher if not already open ──────────────────────
        _log("Step 1: checking if switcher already open...")
        xml = _dump_ui(serial)
        switcher_open = 'id/row_user_textview' in xml

        if switcher_open:
            _log("  -> switcher already open, skipping chevron tap")
            switcher_xml = xml
        else:
            _log("  -> tapping chevron to open switcher")
            chevron_bounds = _find_bounds(xml,
                r'resource-id="com\.instagram\.android:id/action_bar_title_chevron"'
                r'[^>]*bounds="(\[\d+,\d+\]\[\d+,\d+\])"'
            )
            _log(f"  chevron_bounds={chevron_bounds}")
            if not chevron_bounds:
                _log("Step 1 FAILED: chevron not found in XML")
                _log(f"  XML[:400]={xml[:400]}")
                return False

            _tap_bounds(serial, *chevron_bounds)
            time.sleep(2.5)

            # ── Step 2: wait for switcher rows ─────────────────────────────
            _log("Step 2: waiting for switcher rows...")
            switcher_xml = None
            for i in range(8):
                xml = _dump_ui(serial)
                if 'id/row_user_textview' in xml:
                    switcher_xml = xml
                    _log(f"  -> switcher appeared on poll {i}")
                    break
                _log(f"  poll {i}: no switcher yet")
                time.sleep(1)

            if not switcher_xml:
                _log("Step 2 FAILED: switcher never appeared")
                subprocess.run(
                    ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                    capture_output=True, text=True, timeout=5
                )
                return False

        # ── Step 3: parse rows and tap target ──────────────────────────────
        _log("Step 3: parsing switcher rows...")
        rows = _parse_switcher_rows(switcher_xml)
        _log(f"  rows: {[(name, x, y) for name, x, y in rows]}")

        if not rows:
            _log("Step 3 FAILED: no rows parsed")
            _log(f"  XML snippet: {switcher_xml[switcher_xml.find('row_user_textview')-200:switcher_xml.find('row_user_textview')+200]}")
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                capture_output=True, text=True, timeout=5
            )
            return False

        current_lower = current_account.lower() if current_account else None
        target = None

        # Exact name match first
        for name, tx, ty in rows:
            if current_lower and name.lower() == current_lower:
                _log(f"  skipping current: {name}")
                continue
            if name.lower() == account_name.lower():
                target = (name, tx, ty)
                break

        # Fallback: first non-current row
        if target is None:
            for name, tx, ty in rows:
                if current_lower and name.lower() == current_lower:
                    continue
                target = (name, tx, ty)
                break

        if target is None:
            _log("Step 3 FAILED: no target row found")
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                capture_output=True, text=True, timeout=5
            )
            return False

        t_name, t_x, t_y = target
        _log(f"  tapping '{t_name}' at ({t_x},{t_y})")
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "tap", str(t_x), str(t_y)],
            capture_output=True, text=True, timeout=5
        )

        # ── Step 4: wait for switch to complete ────────────────────────────
        time.sleep(7)
        _log("Step 4: verifying...")
        xml = _dump_ui(serial)
        if "unexpected error" in xml.lower():
            _log("  FAILED: unexpected error on screen")
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                capture_output=True, text=True, timeout=5
            )
            return False

        # Confirm Instagram is still running
        top = subprocess.run(
            ["adb", "-s", serial, "shell", "dumpsys", "activity", "top"],
            capture_output=True, text=True, timeout=10
        ).stdout
        ok = INSTAGRAM_PACKAGE in top
        _log(f"  ig_running={ok}")

        # Extra confirmation: check active account name appears in XML
        if ok and account_name.lower() in xml.lower():
            _log(f"  confirmed: '{account_name}' visible in UI")
        elif ok:
            _log(f"  note: '{account_name}' not yet visible in UI (may still be loading)")

        return ok

    except Exception as e:
        print(f"[switch] EXCEPTION: {e}", flush=True)
        import traceback; traceback.print_exc()
        return False

def start_scrcpy(serial: str) -> Optional[subprocess.Popen]:
    scrcpy_bin = SCRCPY_PATH
    if not os.path.isfile(scrcpy_bin):
        found = shutil.which("scrcpy")
        if found:
            scrcpy_bin = found
        else:
            raise FileNotFoundError(
                f"scrcpy not found at '{SCRCPY_PATH}' and not in PATH. "
                "Install scrcpy or update SCRCPY_PATH."
            )

    cmd = [
        scrcpy_bin,
        "-s", serial,
        "--window-title", f"Phone [{serial}]",
        "--stay-awake",
        "--no-audio",
    ]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )

    time.sleep(1.0)
    if proc.poll() is not None:
        _, stderr_bytes = proc.communicate()
        err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"scrcpy exited immediately (code {proc.returncode}). "
            f"stderr: {err_msg[:300]}"
        )
    return proc


def stop_scrcpy(proc: subprocess.Popen) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


class AppiumController:
    def __init__(self, host: str = "127.0.0.1", port: int = 4723):
        self.host = host
        self.port = port
        self.driver: Optional[webdriver.Remote] = None
        self._device_serial: Optional[str] = None

    def start_session(self, device_serial: str) -> bool:
        """
        Attach an Appium/UiAutomator2 session to the already-running Instagram
        process WITHOUT relaunching it.

        get_instagram_accounts() leaves Instagram running before this is called.
        Relaunching here caused a double cold-start that crashed old Instagram.
        Key capabilities that prevent any relaunch:
          autoLaunch=False, dontStopAppOnReset=True, appWaitForLaunch=False.

        If Instagram is somehow not in the foreground we bring it forward with
        am-start --activity-single-top which is safe on a running process.
        """
        print(f"🔧 [DEBUG] Attaching Appium session to running Instagram on {device_serial} port {self.port}")

        self._device_serial = device_serial

        # Bring Instagram to foreground if it somehow ended up in background,
        # without restarting it (--activity-single-top reuses the existing task).
        top = subprocess.run(
            ["adb", "-s", device_serial, "shell", "dumpsys", "activity", "top"],
            capture_output=True, text=True, timeout=10
        )
        if INSTAGRAM_PACKAGE not in top.stdout:
            print(f"📱 [DEBUG] Instagram not in foreground — bringing to front (no restart)")
            subprocess.run(
                ["adb", "-s", device_serial, "shell", "am", "start",
                 "-n", f"{INSTAGRAM_PACKAGE}/com.instagram.mainactivity.InstagramMainActivity",
                 "--activity-single-top"],
                capture_output=True, text=True, timeout=10
            )
            time.sleep(4)

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.udid = device_serial
        options.app_package  = INSTAGRAM_PACKAGE
        options.app_activity = "com.instagram.mainactivity.InstagramMainActivity"
        options.no_reset     = True
        options.auto_grant_permissions = True
        options.new_command_timeout    = 600
        options.uiautomator2_server_launch_timeout = 120000

        # ── Do NOT relaunch the app ─────────────────────────────────────
        options.set_capability("appium:autoLaunch",         False)
        options.set_capability("appium:dontStopAppOnReset", True)
        options.set_capability("appium:appWaitForLaunch",   False)

        # Timeouts for slow Android 7 emulators
        options.set_capability("adbExecTimeout",  240000)
        options.set_capability("appWaitDuration", 240000)

        options.set_capability("appium:skipDeviceInitialization",  True)
        options.set_capability("appium:deviceReadyTimeout",        300)
        options.set_capability("appium:ignoreHiddenApiPolicyError", True)
        options.set_capability("appium:skipServerInstallation",    False)
        options.set_capability("appium:disableWindowAnimation",    False)

        url = f"http://{self.host}:{self.port}"
        self.driver = webdriver.Remote(url, options=options)

        # Short stabilisation pause — Instagram is already running so no
        # long wait needed.
        time.sleep(3)

        print(f"✅ Appium attached to running Instagram on {device_serial}")
        return True

    def stop_session(self):
        """
        Disconnect the Appium session WITHOUT killing Instagram.

        force-stop was removed: killing Instagram here would cause the next
        scraping run / account switch to cold-start the app, which crashes
        old builds.  We only quit the WebDriver (disconnects UiAutomator2)
        and leave Instagram running in the background.
        """
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        # Intentionally NOT calling am force-stop — Instagram stays alive.

    def release_for_adb(self) -> None:
        """
        Temporarily disconnect the UiAutomator2 session so that raw ADB
        commands (uiautomator dump, input tap, keyevent) can operate on the
        accessibility tree without interference.

        WHY THIS IS NECESSARY
        ---------------------
        UiAutomator2 runs a small server on the device that intercepts the
        Android accessibility service.  While that server is connected, ADB
        `uiautomator dump` competes with it for the accessibility lock.  The
        result is that the dump either blocks, returns a stale layout, or
        returns a truncated XML — causing switch_instagram_account()'s Phase B
        to fail to find the chevron even though it is visually on screen.

        This is the root cause of the auto-switch failure:
          - Manual switch works because it runs when the Appium session is
            idle (no active scrape loop) or there is no contention.
          - Auto-switch during scraping fails because the UiAutomator2 server
            is still fully active and `uiautomator dump` gets bad XML.

        After calling this method, call reattach_after_adb() to restore the
        Appium session.  Instagram stays alive throughout — we only disconnect
        the WebDriver client, we do NOT force-stop the app.
        """
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        # Give the UiAutomator2 server a moment to release the accessibility lock
        time.sleep(1.0)

    def reattach_after_adb(self) -> bool:
        """
        Reconnect the Appium/UiAutomator2 session after ADB-based operations
        (e.g. account switching) have completed.

        Instagram must still be running when this is called — release_for_adb()
        guarantees that by NOT force-stopping the app.
        """
        if self._device_serial:
            return self.start_session(self._device_serial)
        return False

    def is_connected(self) -> bool:
        return self.driver is not None

    def press_back(self):
        if self.driver:
            self.driver.back()
            time.sleep(0.5)

    def take_screenshot(self) -> Optional[bytes]:
        if self.driver:
            return self.driver.get_screenshot_as_png()
        return None