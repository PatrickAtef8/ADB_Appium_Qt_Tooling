"""
AppiumManager — auto-starts and stops one Appium server per connected device.
Each device gets its own port starting at BASE_PORT (4723).
"""

import os
import shutil
import subprocess
import time
import threading
import socket
from typing import Dict, Optional, List, Tuple

BASE_PORT = 4723
APPIUM_STARTUP_TIMEOUT = 20

def _find_appium() -> str:
    candidates = [
        shutil.which("appium"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "appium.cmd"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "appium"),
        "/usr/local/bin/appium",
        "/usr/bin/appium",
        os.path.expanduser("~/.npm-global/bin/appium"),
        os.path.expanduser("~/node_modules/.bin/appium"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise FileNotFoundError(
        "Appium not found. Install it with: npm install -g appium\n"
        "Then run: appium driver install uiautomator2"
    )

def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False

def _wait_for_port(port: int, timeout: float = APPIUM_STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.5)
    return False


class AppiumServerHandle:
    def __init__(self, port: int, process: subprocess.Popen):
        self.port = port
        self.process = process

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.is_alive():
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


class AppiumManager:
    def __init__(self, base_port: int = BASE_PORT):
        self.base_port = base_port
        self._servers: Dict[int, Optional[AppiumServerHandle]] = {}
        self._lock = threading.Lock()

    def port_for_index(self, device_index: int) -> int:
        return self.base_port + device_index

    def start_for_devices(self, serials: List[str], log_callback=None) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for i, serial in enumerate(serials):
            port = self.port_for_index(i)
            self._ensure_server(port, log_callback)
            result[serial] = port
        return result

    def stop_all(self) -> None:
        """Fixed: safely handles None sentinels (externally managed ports)."""
        with self._lock:
            for handle in list(self._servers.values()):
                if handle is not None:
                    handle.stop()
            self._servers.clear()

    def stop_for_port(self, port: int) -> None:
        with self._lock:
            handle = self._servers.pop(port, None)
            if handle:
                handle.stop()

    def is_running(self, port: int) -> bool:
        with self._lock:
            h = self._servers.get(port)
            return h is not None and h.is_alive()

    def _ensure_server(self, port: int, log_callback=None) -> None:
        with self._lock:
            existing = self._servers.get(port)
            if existing and existing.is_alive():
                if log_callback:
                    log_callback(f"✅ Appium already running on port {port}")
                return

            if _port_open(port):
                if log_callback:
                    log_callback(f"✅ Port {port} already open — using existing Appium server")
                self._servers[port] = None
                return

            try:
                appium_bin = _find_appium()
            except FileNotFoundError as e:
                raise RuntimeError(str(e))

            if log_callback:
                log_callback(f"🚀 Starting Appium on port {port}...")

            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen(
                [appium_bin, "--port", str(port), "--base-path", "/", "--log-level", "error"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )

            handle = AppiumServerHandle(port, proc)
            self._servers[port] = handle

            ready = _wait_for_port(port, timeout=APPIUM_STARTUP_TIMEOUT)
            if not ready:
                handle.stop()
                with self._lock:
                    self._servers.pop(port, None)
                raise RuntimeError(
                    f"Appium on port {port} did not become ready within {APPIUM_STARTUP_TIMEOUT}s."
                )

            if log_callback:
                log_callback(f"✅ Appium ready on port {port}")