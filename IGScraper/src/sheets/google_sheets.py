"""
Google Sheets integration using gspread + OAuth2.
Handles authentication, appending rows with full data columns, and duplicate prevention.
Also supports webhook export.
"""
import os
import json
import pickle
import threading
from datetime import datetime
from typing import List, Optional

import gspread
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

TOKEN_PATH = os.path.join(os.path.dirname(__file__), "../../config/token.pickle")

HEADERS = [
    "Username",
    "Full Name",
    "Bio",
    "Email",
    "Phone",
    "Country Code",
    "Location",
    "Followers",
    "Following",
    "Posts",
    "Profile URL",
    "Scraped At",
]


def _get_creds(credentials_path: str) -> Credentials:
    """Load or refresh OAuth credentials."""
    creds = None
    token_path = os.path.abspath(TOKEN_PATH)

    if os.path.exists(token_path):
        try:
            with open(token_path, "rb") as f:
                creds = pickle.load(f)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                os.path.abspath(credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=True)

        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return creds


class SheetsClient:
    def __init__(self, credentials_path: str, sheet_id: str, tab_name: str = "Sheet1"):
        self.credentials_path = os.path.abspath(credentials_path)
        self.sheet_id = sheet_id
        self.tab_name = tab_name
        self._client: Optional[gspread.Client] = None
        self._worksheet: Optional[gspread.Worksheet] = None
        self._known_usernames: set = set()
        self._lock = threading.Lock()

    def authenticate(self) -> bool:
        """Run OAuth flow and store token. Returns True on success."""
        creds = _get_creds(self.credentials_path)
        self._client = gspread.authorize(creds)
        return True

    def connect_sheet(self) -> bool:
        """Open the spreadsheet and prepare the worksheet."""
        if not self._client:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        spreadsheet = self._client.open_by_key(self.sheet_id)

        try:
            self._worksheet = spreadsheet.worksheet(self.tab_name)
        except gspread.WorksheetNotFound:
            self._worksheet = spreadsheet.add_worksheet(
                title=self.tab_name, rows=5000, cols=len(HEADERS) + 5
            )

        # Ensure headers exist
        existing = self._worksheet.row_values(1)
        if existing != HEADERS:
            self._worksheet.delete_rows(1)
            self._worksheet.insert_row(HEADERS, 1)

        # Load existing usernames for dedup
        try:
            all_usernames = self._worksheet.col_values(1)[1:]  # skip header
            self._known_usernames = set(u.lower() for u in all_usernames if u)
        except Exception:
            self._known_usernames = set()

        return True

    def append_account(self, account: dict) -> bool:
        """
        Append an account row. Returns False if duplicate.
        Retries up to 3 times on transient API errors (e.g. 503 Service Unavailable).
        """
        if not self._worksheet:
            raise RuntimeError("Sheet not connected.")

        username = account.get("username", "").lower()

        with self._lock:
            if username in self._known_usernames:
                return False

            row = [
                account.get("username", ""),
                account.get("full_name", ""),
                account.get("bio", ""),
                account.get("email", ""),
                account.get("phone", ""),
                account.get("country_code", ""),
                account.get("location", ""),
                str(account.get("followers", "")),
                str(account.get("following", "")),
                str(account.get("post_count", "")),
                account.get("profile_url", ""),
                account.get("scraped_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ]

            last_exc = None
            for attempt in range(3):
                try:
                    self._worksheet.append_row(row, value_input_option="USER_ENTERED")
                    self._known_usernames.add(username)
                    return True
                except gspread.exceptions.APIError as exc:
                    last_exc = exc
                    # Only retry on transient server-side errors (5xx).
                    # 4xx errors (bad request, auth) are permanent — raise immediately.
                    status = getattr(exc, "response", None)
                    code = status.status_code if status is not None else 0
                    if code and code < 500:
                        raise
                    import time as _time
                    _time.sleep(2 ** attempt)   # 1s, 2s, 4s back-off

            raise last_exc

    def get_row_count(self) -> int:
        if not self._worksheet:
            return 0
        return max(0, len(self._worksheet.col_values(1)) - 1)

    def revoke_token(self):
        """Delete saved token to force re-auth."""
        token_path = os.path.abspath(TOKEN_PATH)
        if os.path.exists(token_path):
            os.remove(token_path)


def send_webhook(webhook_url: str, account: dict) -> bool:
    """
    POST account data to a webhook URL as JSON.
    Returns True on success (2xx response).
    """
    if not webhook_url:
        return False
    try:
        import urllib.request
        payload = json.dumps(account, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
