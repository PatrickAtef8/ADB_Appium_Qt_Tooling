"""
Persistent blacklist manager — stores scraped/skipped usernames to disk.
"""
import os
import json

BLACKLIST_PATH = os.path.join(os.path.dirname(__file__), "../../config/blacklist.json")


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
