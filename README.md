# Instagram Scraper Pro

A Windows desktop application that collects publicly visible Instagram profile data
(username, email, phone, country, posts, etc.) from followers/following lists
and exports to Google Sheets + optional webhook.

---

## Features

- **Multi-phone support** — up to 3 Android phones via USB
- **Account switching** — automatically rotates Instagram accounts on each phone
- **Working hours scheduler** — only runs between configurable hours (e.g. 09:00–19:00)
- **Randomized intervals** — every delay has MIN/MAX bounds for human-like behavior
- **Full data extraction** — username, full name, email, phone, country code (from phone prefix/location), location, followers, following, post count, bio
- **Smart filters** — skip by: no bio, private, no profile pic, no contact info, min posts, no recent activity, keywords
- **Persistent blacklist** — scraped usernames never processed again across sessions
- **Google Sheets export** — with all data columns in predefined order
- **Webhook export** — POST each account as JSON to any webhook URL
- **CSV export** — download results from the UI
- **Scrcpy integration** — view phone screen at `/usr/local/bin/scrcpy`
- **Dark/light theme** toggle

---

## Requirements

### On your Windows machine
- Python 3.10+
- Node.js 18+ (for Appium)
- ADB (Android Debug Bridge) — in PATH
- Appium 2.x with UIAutomator2 driver
- scrcpy (optional, for phone screen viewing)

### On each Android phone
- Instagram installed and logged in (can be multiple accounts)
- USB Debugging enabled: Settings → Developer Options → USB Debugging
- Trust the computer when prompted

---

## Installation

```bat
:: 1. Clone / unzip the project
cd upworkAutomation

:: 2. Create virtual environment
python -m venv venv
venv\Scripts\activate

:: 3. Install Python dependencies
pip install -r requirements.txt

:: 4. Install Appium + UIAutomator2
npm install -g appium
appium driver install uiautomator2

:: 5. Verify ADB sees your devices
adb devices
```

---

## Google Sheets Setup

1. Go to https://console.cloud.google.com
2. Create a new project (or use existing)
3. Enable: **Google Sheets API** and **Google Drive API**
4. Go to Credentials → Create OAuth 2.0 Client ID → Desktop App
5. Download the JSON file → save as `assets/credentials.json`
6. Create a new Google Sheet, copy the ID from the URL:
   ```
   https://docs.google.com/spreadsheets/d/THIS_IS_THE_ID/edit
   ```

---

## Running

```bat
:: 1. Start Appium server (keep this running)
appium

:: 2. In a new terminal, launch the GUI
venv\Scripts\activate
python main.py
```

---

## Using the App

### Dashboard
1. Click **Refresh Devices** — your connected phones appear in Phone 1/2/3 slots
2. Click **👁 View** next to any phone to open scrcpy (see the screen)
3. Enter target Instagram usernames (one per line) in the Target List box
4. Set mode (followers/following) and max accounts per target
5. Optionally enable working hours schedule
6. Click **▶ START** — the app navigates to Results tab automatically

### Filters & Blacklist
- Configure all skip conditions (no bio, private, no contact, min posts, keywords)
- View and edit the persistent blacklist (auto-updated after each scraped profile)

### Settings
- Paste your Google Sheet ID and connect (browser OAuth window will open)
- Add webhook URL for real-time POST notifications
- Tune all randomized delay MIN/MAX values

---

## Google Sheets Column Order

| Column | Description |
|--------|-------------|
| Username | Instagram @handle |
| Full Name | Display name |
| Bio | Profile biography |
| Email | Extracted from bio/contact |
| Phone | Extracted from bio/contact |
| Country Code | 2-letter ISO code (from phone prefix or location) |
| Location | Location text from profile |
| Followers | Follower count |
| Following | Following count |
| Posts | Post count |
| Profile URL | Direct link |
| Scraped At | Timestamp |

---

## Building the Windows EXE

### Option A: GitHub Actions (recommended, no Wine needed)
```bat
git init
git remote add origin https://github.com/YOUR_USERNAME/upworkAutomation.git
git add .
git commit -m "Initial commit"
git push -u origin main
```
GitHub builds the `.exe` automatically. Download from the Releases section.

### Option B: Local build on Windows
```bat
pip install pyinstaller
pyinstaller instagram_scraper.spec
:: EXE will be in dist/InstagramScraperPro.exe
```

---

## Project Structure

```
upworkAutomation/
├── main.py                           # Entry point
├── requirements.txt
├── instagram_scraper.spec            # PyInstaller config
├── .github/workflows/build.yml       # Auto-build EXE
├── config/
│   ├── settings.json                 # Auto-saved config
│   └── blacklist.json                # Persistent blacklist
├── assets/
│   └── credentials.json              # Google OAuth credentials
└── src/
    ├── ui/
    │   └── main_window.py            # Full PyQt6 GUI
    ├── automation/
    │   ├── appium_controller.py      # ADB, Appium, account switching, scrcpy
    │   └── scraper.py                # Instagram navigation & data extraction
    ├── sheets/
    │   └── google_sheets.py          # Google Sheets + webhook
    └── utils/
        ├── config_manager.py         # Load/save settings.json
        ├── filters.py                # Filter logic, email/phone/country extraction
        └── blacklist.py              # Persistent blacklist (blacklist.json)
```

---

## Troubleshooting

**"No devices found"** → Run `adb devices` in terminal. USB Debugging must be on.

**Appium session fails** → Ensure `appium` is running in a separate terminal.

**Instagram elements not found** → Instagram updates its UI. Update resource IDs in `scraper.py` using Appium Inspector.

**Google auth fails** → Go to Settings → click "Revoke Token" → reconnect to re-authenticate.

**scrcpy not found** → Ensure scrcpy is installed at `/usr/local/bin/scrcpy`. On Windows, adjust `SCRCPY_PATH` in `appium_controller.py`.

---

## Developer Notes

- All delays are randomized: `between_profiles_min/max`, `between_scrolls_min/max`, `rest_min/max_minutes`, `run_min/max_profiles`
- The scheduler checks time every 60 seconds; the scraper blocks cleanly during off-hours
- Account switching uses ADB tap automation on the Instagram switcher UI
- Email/phone extracted via regex from bio text and the Contact button sheet
- Country code inferred from phone international prefix (3→2→1 digit lookup) or location text keywords
- Blacklist is a flat JSON array (`config/blacklist.json`), loaded once per session and updated after each successful scrape
- Webhook sends are fire-and-forget in a daemon thread (non-blocking)
