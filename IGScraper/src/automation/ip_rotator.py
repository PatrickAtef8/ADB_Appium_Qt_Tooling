"""
ip_rotator.py — Rotate mobile-data IP by toggling airplane mode or svc data.

Strategy (Android):
  1. Disable mobile data via ADB shell svc data disable
  2. Wait random reconnect_wait seconds
  3. Re-enable mobile data via ADB shell svc data enable
  4. Wait extra stabilise seconds for IP to be assigned
  5. Log the cycle

Works ONLY on real phones with an active SIM on mobile data.
Does NOT work on Wi-Fi-only connections or emulators.

Usage (called from PhoneWorker or MainAccountWorker):
    rotator = IPRotator(serial="emulator-5554", log_callback=self._log)
    rotator.rotate()   # blocks until done
"""

import time
import random
import subprocess
from typing import Callable, Optional


def _adb(serial: str, *args, timeout: int = 15) -> str:
    """Run an adb shell command and return stdout (stripped). Never raises."""
    try:
        result = subprocess.run(
            ["adb", "-s", serial, "shell"] + list(args),
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except Exception:
        return ""


class IPRotator:
    """
    Manages timed IP rotation for a single device serial.

    Parameters
    ----------
    serial       : ADB device serial
    interval_min : minimum minutes between rotations
    interval_max : maximum minutes between rotations
    log_callback : optional callable(str) for log messages
    """

    def __init__(
        self,
        serial: str,
        interval_min: float = 5.0,
        interval_max: float = 15.0,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.serial       = serial
        self.interval_min = interval_min
        self.interval_max = interval_max
        self._log_cb      = log_callback
        self._last_rotate = 0.0      # epoch time of last rotation
        self._next_in     = self._pick_interval()

    # ── Public API ────────────────────────────────────────────────────────

    def tick(self) -> bool:
        """
        Call this regularly (e.g. every 30 s) from the worker loop.
        Returns True if a rotation was performed this tick.
        """
        elapsed = time.time() - self._last_rotate
        if self._last_rotate > 0 and elapsed < self._next_in:
            return False
        self.rotate()
        return True

    def rotate(self) -> bool:
        """
        Perform one IP rotation cycle immediately.
        Blocks for ~10–25 s while mobile data reconnects.
        Returns True on apparent success, False if ADB commands failed.
        """
        self._log(f"🔄 [IP Rotate] Starting rotation on {self.serial}…")

        # --- Step 1: disable mobile data ---------------------------------
        _adb(self.serial, "svc", "data", "disable")
        self._log("📴 [IP Rotate] Mobile data disabled.")

        # --- Step 2: wait for disconnect ---------------------------------
        wait_off = random.uniform(4.0, 9.0)
        self._log(f"⏳ [IP Rotate] Waiting {wait_off:.1f}s for disconnect…")
        time.sleep(wait_off)

        # --- Step 3: re-enable mobile data --------------------------------
        _adb(self.serial, "svc", "data", "enable")
        self._log("📶 [IP Rotate] Mobile data re-enabled.")

        # --- Step 4: stabilise -------------------------------------------
        stabilise = random.uniform(6.0, 12.0)
        self._log(f"⏳ [IP Rotate] Waiting {stabilise:.1f}s for new IP assignment…")
        time.sleep(stabilise)

        # --- Step 5: verify connectivity (optional ping) -----------------
        ok = self._verify_connectivity()
        if ok:
            self._log("✅ [IP Rotate] IP rotation complete — device is online.")
        else:
            self._log("⚠️ [IP Rotate] Device may still be reconnecting — continuing anyway.")

        self._last_rotate = time.time()
        self._next_in     = self._pick_interval()
        self._log(
            f"⏱️ [IP Rotate] Next rotation in "
            f"{self._next_in / 60:.1f} min."
        )
        return ok

    def reset_timer(self):
        """Call after a manual rotation or session restart to reset the clock."""
        self._last_rotate = time.time()
        self._next_in     = self._pick_interval()

    def seconds_until_next(self) -> float:
        """How many seconds until the next scheduled rotation."""
        if self._last_rotate == 0:
            return 0.0
        return max(0.0, self._next_in - (time.time() - self._last_rotate))

    # ── Private helpers ───────────────────────────────────────────────────

    def _pick_interval(self) -> float:
        """Return a random interval in seconds within [interval_min, interval_max]."""
        return random.uniform(
            self.interval_min * 60,
            self.interval_max * 60,
        )

    def _verify_connectivity(self) -> bool:
        """Ping 8.8.8.8 once via ADB; return True if reachable."""
        out = _adb(self.serial, "ping", "-c", "1", "-W", "3", "8.8.8.8", timeout=10)
        return "1 received" in out or "1 packets received" in out

    def _log(self, msg: str):
        if self._log_cb:
            self._log_cb(msg)
