"""
completed.py — Tracks fully-scraped target accounts.

Writes a human-readable summary to config/scraping_summary.txt that the
client can open in any text editor or download from the app.  Every run
appends a new block so the full history is preserved.
"""
import os
import sys
from datetime import datetime


def _resolve_summary_path() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(base, "config", "scraping_summary.txt")


SUMMARY_PATH = _resolve_summary_path()

# ── In-memory session state (reset each run) ──────────────────────────────────

_session_start:  str  = ""
_session_counts: dict = {}   # phone_label -> int
_session_done:   list = []   # [{username, phone, completed_at}]


def start_session(phone_labels: dict) -> None:
    """
    Call at the beginning of a scraping run.
    phone_labels: {phone_idx (int): label (str)}
    """
    global _session_start, _session_counts, _session_done
    _session_start  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _session_counts = {label: 0 for label in phone_labels.values()}
    _session_done   = []


def record_scraped(phone_label: str, count: int) -> None:
    """Update the running account count for a phone."""
    _session_counts[phone_label] = count


def mark_target_completed(username: str, phone_label: str) -> None:
    """Record a fully-scraped target (called when target_done signal fires)."""
    _session_done.append({
        "username":     username.lower().strip(),
        "phone":        phone_label,
        "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def finish_session(total_collected: int) -> None:
    """Append a formatted block to scraping_summary.txt."""
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "=" * 60,
        "  SCRAPING SESSION",
        f"  Started  : {_session_start}",
        f"  Finished : {end_time}",
        f"  Total accounts collected: {total_collected}",
        "=" * 60,
        "",
        "  Per-Phone Summary:",
    ]
    for label, count in _session_counts.items():
        lines.append(f"    • {label}: {count} accounts scraped")

    if _session_done:
        lines.append("")
        lines.append("  Completed Targets:")
        for entry in _session_done:
            lines.append(
                f"    v @{entry['username']}  --  {entry['phone']}  --  {entry['completed_at']}"
            )
    else:
        lines.append("")
        lines.append("  Completed Targets: none")

    lines += ["", ""]

    path = os.path.abspath(SUMMARY_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def get_summary_path() -> str:
    return os.path.abspath(SUMMARY_PATH)


def summary_exists() -> bool:
    return os.path.exists(os.path.abspath(SUMMARY_PATH))


def get_completed_usernames() -> set:
    """Return set of all completed usernames from the txt summary."""
    if not summary_exists():
        return set()
    usernames = set()
    try:
        with open(os.path.abspath(SUMMARY_PATH), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("v @"):
                    part = line[3:].split("  --  ")[0].strip()
                    if part:
                        usernames.add(part.lower())
    except Exception:
        pass
    return usernames
