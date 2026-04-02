"""
Config manager — load/save settings.json with deep merge.

Path resolution (priority order):
  1. Frozen EXE on Windows → %APPDATA%\Cansa\settings.json
     (_MEIPASS is a temp dir that is deleted between runs, so we must NOT write there)
  2. Frozen EXE on Linux   → ~/.config/Cansa/settings.json
  3. Normal Python run     → <project_root>/config/settings.json  (original behaviour)
"""
import json
import os
import sys
import copy


def _get_config_path() -> str:
    if getattr(sys, "_MEIPASS", None):
        # Running as a frozen PyInstaller EXE — use a persistent user directory.
        if sys.platform == "win32":
            base = os.environ.get("APPDATA", os.path.expanduser("~"))
        else:
            base = os.path.join(os.path.expanduser("~"), ".config")
        return os.path.join(base, "Cansa", "settings.json")
    # Normal source run — original relative path.
    return os.path.join(os.path.dirname(__file__), "../../config/settings.json")


CONFIG_PATH = _get_config_path()

DEFAULT_CONFIG = {
    "sheet_id": "",
    "sheet_tab": "Sheet1",
    "credentials_path": "assets/credentials.json",
    "webhook_url": "",
    "filters": {
        "keywords": [],
        "skip_no_bio": False,
        "skip_private": False,
        "skip_no_profile_pic": False,
        "skip_no_contact": False,
        "min_posts": 0,
        "require_recent_post_days": 365,
    },
    "delays": {
        "between_profiles_min": 2.0,
        "between_profiles_max": 4.0,
        "between_scrolls_min": 1.0,
        "between_scrolls_max": 3.0,
        "session_break_every": 100,
        "session_break_duration": 30,
        "run_min_profiles": 3,
        "run_max_profiles": 10,
        "rest_min_minutes": 30,
        "rest_max_minutes": 60,
    },
    "schedule": {
        "enabled": False,
        "start_hour": 9,
        "start_minute": 0,
        "end_hour": 19,
        "end_minute": 0,
    },
    "appium": {
        "host": "127.0.0.1",
        "port": 4723,
    },
    "devices": [],
    "target_list": [],
    "blacklist": [],
    "last_target": "",
    "last_mode": "followers",
    "last_count": 100,
    "last_device": "",
}


def load_config() -> dict:
    path = os.path.abspath(CONFIG_PATH)
    if not os.path.exists(path):
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), data)
        return merged
    except Exception:
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    path = os.path.abspath(CONFIG_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base