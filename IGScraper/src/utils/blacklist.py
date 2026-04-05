"""
Persistent blacklist manager — stores scraped/skipped usernames to disk.
"""
import os
import sys
import json


def _resolve_blacklist_path() -> str:
    """
    Return an absolute path to config/blacklist.json that is stable
    regardless of the working directory or whether the app is run as
    a plain Python script or a PyInstaller-frozen executable.

    PyInstaller sets sys.frozen=True and sys.executable to the .exe path.
    In that case the config folder lives next to the executable.
    When run as a normal script, we walk up from this file's location to
    the project root (the directory that contains the 'config' folder).
    Using __file__-relative traversal fixes the original bug where
    os.getcwd()-based resolution produced a different path depending on
    which directory the app was launched from.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller bundle — config/ sits beside the .exe
        base = os.path.dirname(sys.executable)
    else:
        # Normal Python — go up from src/utils/ → src/ → project root
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(base, "config", "blacklist.json")


BLACKLIST_PATH = _resolve_blacklist_path()


def load_blacklist() -> set:
    path = os.path.abspath(BLACKLIST_PATH)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(u.lower() for u in data if u)
    except Exception:
        return set()


def save_blacklist(blacklist: set) -> None:
    path = os.path.abspath(BLACKLIST_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(blacklist), f, indent=2, ensure_ascii=False)


def add_to_blacklist(username: str) -> None:
    bl = load_blacklist()
    bl.add(username.lower().strip())
    save_blacklist(bl)


def add_many_to_blacklist(usernames) -> None:
    bl = load_blacklist()
    for u in usernames:
        if u:
            bl.add(u.lower().strip())
    save_blacklist(bl)


def clear_blacklist() -> None:
    save_blacklist(set())
