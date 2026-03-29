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
    Detect all logged-in Instagram accounts by opening the in-app account
    switcher and reading the UI hierarchy.

    - Opens Instagram if not already running.
    - Navigates to the profile tab.
    - Taps the chevron to open the account switcher bottom sheet.
    - Reads all account rows from the live UI dump.
    - Closes the sheet with Back.

    No hardcoded coordinates. Works on any screen size without root.
    Confirmed working on Android 7 emulator.
    """
    accounts = []
    try:
        # ── Step 1: open Instagram ────────────────────────────────────────────
        subprocess.run(
            ["adb", "-s", serial, "shell", "am", "start",
             "-n", f"{INSTAGRAM_PACKAGE}/.activity.MainTabActivity"],
            capture_output=True, text=True, timeout=10
        )
        time.sleep(4)

        # ── Step 2: navigate to profile tab ──────────────────────────────────
        subprocess.run(
            ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/_ig_ui.xml"],
            capture_output=True, text=True, timeout=10
        )
        xml_result = subprocess.run(
            ["adb", "-s", serial, "shell", "cat", "/sdcard/_ig_ui.xml"],
            capture_output=True, text=True, timeout=10
        )
        xml = xml_result.stdout

        profile_tab = re.search(
            r'content-desc="Profile"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml
        )
        if profile_tab:
            px = (int(profile_tab.group(1)) + int(profile_tab.group(3))) // 2
            py = (int(profile_tab.group(2)) + int(profile_tab.group(4))) // 2
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "tap", str(px), str(py)],
                capture_output=True, text=True, timeout=5
            )
            time.sleep(3)

        # ── Step 3: find chevron and open switcher ────────────────────────────
        subprocess.run(
            ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/_ig_ui.xml"],
            capture_output=True, text=True, timeout=10
        )
        xml_result = subprocess.run(
            ["adb", "-s", serial, "shell", "cat", "/sdcard/_ig_ui.xml"],
            capture_output=True, text=True, timeout=10
        )
        xml = xml_result.stdout

        chevron = re.search(
            r'resource-id="com\.instagram\.android:id/action_bar_title_chevron"'
            r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml
        )
        if not chevron:
            return ["Account 1"]

        cx = (int(chevron.group(1)) + int(chevron.group(3))) // 2
        cy = (int(chevron.group(2)) + int(chevron.group(4))) // 2
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "tap", str(cx), str(cy)],
            capture_output=True, text=True, timeout=5
        )
        time.sleep(3)

        # ── Step 4: dump switcher and extract usernames ───────────────────────
        subprocess.run(
            ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/_ig_ui.xml"],
            capture_output=True, text=True, timeout=10
        )
        xml_result = subprocess.run(
            ["adb", "-s", serial, "shell", "cat", "/sdcard/_ig_ui.xml"],
            capture_output=True, text=True, timeout=10
        )
        switcher_xml = xml_result.stdout

        # Use the same imageview-anchor row parser used by switch_instagram_account
        account_rows = _parse_account_rows(switcher_xml)
        accounts = [name for name, _ in account_rows]

        # ── Step 5: close the switcher ────────────────────────────────────────
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

        bounds = _find_bounds(xml,
            r'content-desc="Profile"[^>]*bounds="(\[\d+,\d+\]\[\d+,\d+\])"'
        ) or _find_bounds(xml,
            r'resource-id="com\.instagram\.android:id/profile_tab"'
            r'[^>]*bounds="(\[\d+,\d+\]\[\d+,\d+\])"'
        )

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
    every account except 'Add account'.

    Strategy — tap directly on the row_user_imageview (avatar):
      Each account row contains a row_user_imageview whose bounds are
      unique to that row. Tapping the centre of the imageview always
      activates the correct account row — no container-matching needed,
      no ambiguity from overlapping full-width elements.

    This is 100% generic: works on any screen size, density, or device.
    """
    SKIP = {"add account"}

    segments = re.split(
        r'(?=resource-id="com\.instagram\.android:id/row_user_imageview")',
        xml
    )

    results: List[Tuple[str, Tuple[int,int,int,int]]] = []

    for seg in segments:
        # Get the imageview bounds — this IS the tap target
        img_m = re.search(
            r'resource-id="com\.instagram\.android:id/row_user_imageview"'
            r'[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            seg
        )
        if not img_m:
            continue

        img_bounds = (
            int(img_m.group(1)), int(img_m.group(2)),
            int(img_m.group(3)), int(img_m.group(4))
        )

        # Get the username from the same segment
        name_m = (
            re.search(
                r'text="([^"]+)"[^>]*'
                r'resource-id="com\.instagram\.android:id/row_user_textview"',
                seg
            ) or
            re.search(
                r'resource-id="com\.instagram\.android:id/row_user_textview"'
                r'[^>]*text="([^"]+)"',
                seg
            )
        )
        if not name_m:
            continue

        name = name_m.group(1).strip()
        if not name or name.lower() in SKIP:
            continue

        if name not in [r[0] for r in results]:
            results.append((name, img_bounds))

    return results


def switch_instagram_account(serial: str, account_name: str) -> bool:
    """
    Switch Instagram to the account with the given username.

    Uses the account NAME (not index) so the correct row is always tapped
    regardless of the order accounts appear in the switcher — which can
    change depending on which account is currently active.

    Flow:
      1. Launch Instagram via monkey.
      2. Navigate to profile tab + open account switcher (with retries).
      3. Parse accounts using imageview-anchor row detection.
      4. Find the row whose name matches account_name (case-insensitive).
      5. Tap that row's imageview bounds directly.

    Returns True on success, False on any failure.
    """
    try:
        # Launch Instagram
        subprocess.run(
            ["adb", "-s", serial, "shell",
             "monkey", "-p", INSTAGRAM_PACKAGE,
             "-c", "android.intent.category.LAUNCHER", "1"],
            capture_output=True, text=True, timeout=10
        )
        for _ in range(10):
            time.sleep(1)
            top = subprocess.run(
                ["adb", "-s", serial, "shell", "dumpsys", "activity", "top"],
                capture_output=True, text=True, timeout=10
            )
            if INSTAGRAM_PACKAGE in top.stdout:
                break
        time.sleep(2)

        # Navigate to profile tab and open switcher
        switcher_xml = _go_to_profile_and_open_switcher(serial)
        if not switcher_xml:
            return False

        # Parse account rows
        account_rows = _parse_account_rows(switcher_xml)
        if not account_rows:
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                capture_output=True, text=True, timeout=5
            )
            return False

        # Find the row matching the target account name (case-insensitive)
        target_bounds = None
        for name, bounds in account_rows:
            if name.lower() == account_name.lower():
                target_bounds = bounds
                break

        if target_bounds is None:
            subprocess.run(
                ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
                capture_output=True, text=True, timeout=5
            )
            return False

        x1, y1, x2, y2 = target_bounds
        tap_x = (x1 + x2) // 2
        tap_y = (y1 + y2) // 2

        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "tap", str(tap_x), str(tap_y)],
            capture_output=True, text=True, timeout=5
        )
        time.sleep(5)
        return True

    except Exception:
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
        """Start Appium session with extra robustness for Android 7 + old Instagram."""
        print(f"🔧 [DEBUG] Starting Appium session for {device_serial} on port {self.port} (FORCED MAIN ACTIVITY + 4min timeout)")

        self._device_serial = device_serial

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.udid = device_serial
        options.app_package = INSTAGRAM_PACKAGE
        options.app_activity = "com.instagram.mainactivity.InstagramMainActivity"  # ← FORCED
        options.no_reset = True
        options.auto_grant_permissions = True
        options.new_command_timeout = 600
        options.uiautomator2_server_launch_timeout = 120000

        # === MAX TIMEOUTS FOR SLOW ANDROID 7 EMULATORS ===
        options.set_capability("adbExecTimeout", 240000)      # 4 minutes
        options.set_capability("appWaitDuration", 240000)
        options.set_capability("appWaitForLaunch", False)

        options.set_capability("appium:skipDeviceInitialization", True)
        options.set_capability("appium:deviceReadyTimeout", 300)
        options.set_capability("appium:ignoreHiddenApiPolicyError", True)
        options.set_capability("appium:skipServerInstallation", False)
        options.set_capability("appium:disableWindowAnimation", False)

        url = f"http://{self.host}:{self.port}"
        self.driver = webdriver.Remote(url, options=options)

        # === EXTRA STEP: Force Instagram to foreground and wait ===
        print(f"📱 [DEBUG] Activating Instagram on {device_serial}...")
        self.driver.activate_app(INSTAGRAM_PACKAGE)
        time.sleep(8)  # give old Instagram time to fully load UI

        print(f"✅ Appium session + Instagram ready for {device_serial}")
        return True

    def stop_session(self):
        """Stop session and clean state."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

        if self._device_serial:
            try:
                print(f"🧹 Force-stopping Instagram on {self._device_serial}")
                subprocess.run(
                    ["adb", "-s", self._device_serial, "shell", "am", "force-stop", INSTAGRAM_PACKAGE],
                    capture_output=True, timeout=10
                )
                time.sleep(2)   # longer breathing room for old emulators
            except Exception:
                pass

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