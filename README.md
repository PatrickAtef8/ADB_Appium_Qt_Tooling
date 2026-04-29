# IGScraper

A desktop application for collecting publicly visible Instagram profile data
from followers and following lists, with multi-device Android automation, a live
screen mirror, and export to Google Sheets, webhook, and CSV.

---

## Screenshots

<p align="center">
  <img src="images/1.png" width="48%"/>
  <img src="images/2.png" width="48%"/>
</p>
<p align="center">
  <img src="images/3.png" width="48%"/>
  <img src="images/4.png" width="48%"/>
</p>
<p align="center">
  <img src="images/5.png" width="48%"/>
  <img src="images/6.png" width="48%"/>
</p>
<p align="center">
  <img src="images/7.png" width="48%"/>
  <img src="images/8.png" width="48%"/>
</p>
<p align="center">
  <img src="images/9.png" width="48%"/>
  <img src="images/10.png" width="48%"/>
  <img src="images/11.png" width="48%"/>
</p>

---

## Features

### Multi-device scraping

Up to 10 Android phones or emulators can run in parallel, each on its own Appium
session. Slots are added and removed from the dashboard with + and - buttons, and
each slot can be given a nickname to tell devices apart at a glance.

### Account switching

The app rotates Instagram accounts automatically on each device. Two modes are
available: switch after every N profiles collected, or switch after every N hours.
The switcher handles both resource-ID-based Instagram versions and newer ID-stripped
versions using a dual-strategy parser.

### Working hours scheduler

Scraping only runs inside a configured time window (for example, 09:00 to 19:00).
Outside that window the workers sleep and resume automatically on the next cycle.

### Randomized delays

Every wait in the automation has a configurable MIN and MAX: time between profiles,
time between list scrolls, rest duration, and session break frequency. All values are
drawn randomly within those bounds on each use to simulate human-like pacing.

### Data extraction

For every profile the scraper collects:

- Username and full name
- Bio text
- Email address (from bio and the Contact bottom sheet)
- Phone number (from bio and the Contact bottom sheet)
- Country code, inferred first from international phone prefix (3-digit → 2-digit →
  1-digit lookup) and then from location text keywords, with word-boundary regex to
  prevent false positives on short country names embedded in longer words
- Location / address, pulled from the dedicated profile header location element and
  the business address element; if both are empty, the bio is scanned for lines that
  start with a pin emoji as a fallback; if location is still empty after all of that
  but a country code was inferred from the phone number, the full country name is
  written to the location field automatically
- Follower count, following count, post count
- Profile picture presence (pixel-sampled from a screenshot)
- Story ring presence
- Most recent post date

### Filters

Profiles can be skipped based on any combination of the following conditions:

- No bio
- Account is private
- No profile picture
- No contact information (email or phone)
- Post count below a minimum threshold
- No post activity within the last N months (stories count as recent activity)
- Bio contains one or more blacklist keywords (matched with word boundaries)
- Bio or username does not contain any of a required keyword list

### Keyword-based blacklist

Accounts that match keyword filters are added to a separate keyword blacklist so they
are not re-evaluated on future runs. The main blacklist tracks every successfully
scraped username and prevents duplicate processing across sessions.

### Completed-target tracking

When all profiles from a target username have been collected, the target is recorded
in `config/scraping_summary.txt` with the phone label and a timestamp. Completed
targets are skipped automatically on the next run.

### Google Sheets export

Results are appended to a configured Google Sheet in the following column order:

| Column | Content |
|---|---|
| Username | Instagram handle |
| Full Name | Display name |
| Bio | Profile biography |
| Email | Extracted contact email |
| Phone | Extracted contact phone |
| Country Code | ISO 2-letter code |
| Location | Location / address text |
| Followers | Follower count |
| Following | Following count |
| Posts | Post count |
| Profile URL | Direct link |
| Scraped At | Timestamp |

OAuth authentication runs in a background thread so the UI stays responsive during
the browser consent flow.

### Webhook export

Each scraped account is also POSTed as JSON to any webhook URL (Zapier, Make,
custom endpoint, etc.). Sends are fire-and-forget in a daemon thread and do not
block the scraper.

### CSV export

Results can be downloaded to a local CSV file directly from the Results tab.

### Live screen mirror

A resizable panel on the right side of the window streams the Android device screen
in real time using scrcpy. The panel width is adjustable with a drag grip on its left
edge and with step buttons, and the last-used width is saved across sessions. The
mirror can be attached to any of the active device slots.

### IP rotation

An optional per-device IP rotator can toggle airplane mode (or mobile data) on a
randomized interval to cycle the device's IP address between scraping sessions.

### Main Account mode

One phone slot can be designated as the Main Account. Instead of scraping, that slot
runs an engagement loop: it watches stories, scrolls the feed, and browses reels.
Each action (like, react, comment) has an independent enable toggle and a percentage
chance. Story replies can be generated from a spintax template pool or via an OpenAI
API key. The Main Account worker respects its own working-hours windows independently
from the scraping schedule.

### UI

The interface is built with PyQt6 and QFluentWidgets (Fluent Design System) and
supports light and dark themes with a single toggle. Typography and spacing scale with
the system DPI so the layout is usable at any display scaling factor. Notifications
appear as non-intrusive info bars rather than blocking dialogs.

---

## Requirements

### On the Windows machine

- Python 3.10+
- Node.js 18+ (for Appium)
- ADB in PATH
- Appium 2.x with the UIAutomator2 driver
- scrcpy (optional, for live screen mirror)

### On each Android device

- Instagram installed and logged in (multiple accounts are supported)
- USB Debugging enabled: Settings → Developer Options → USB Debugging
- Trust the PC when prompted

---

## Installation

```bat
:: 1. Unzip or clone the project
cd IGScraper

:: 2. Create a virtual environment
python -m venv venv
venv\Scripts\activate

:: 3. Install Python dependencies
pip install -r requirements.txt

:: 4. Install Appium and the UIAutomator2 driver
npm install -g appium
appium driver install uiautomator2

:: 5. Verify ADB sees your devices
adb devices
```

---

## Google Sheets setup

1. Go to https://console.cloud.google.com and create or select a project.
2. Enable the Google Sheets API and the Google Drive API.
3. Go to Credentials → Create OAuth 2.0 Client ID → Desktop App.
4. Download the JSON file and save it as `assets/credentials.json`.
5. Create a Google Sheet and copy the spreadsheet ID from its URL:
   ```
   https://docs.google.com/spreadsheets/d/THIS_IS_THE_ID/edit
   ```
6. Paste the ID into the Settings page and click Connect. A browser window will open
   for the OAuth consent flow.

---

## Running

```bat
:: Terminal 1 — keep this running
appium

:: Terminal 2
venv\Scripts\activate
python main.py
```

---

## Using the app

### Dashboard

1. Click Refresh Devices. Connected phones appear in the phone slots.
2. Use the + and - buttons to add or remove slots (up to 10).
3. Assign each slot a nickname if needed.
4. Enter target Instagram usernames (one per line) in the Targets field for each slot.
5. Set the scraping mode (followers or following) and the maximum number of profiles.
6. Enable the working hours schedule if needed.
7. Click Start. The app switches to the Results tab automatically.

### Filters and blacklist

Configure all skip conditions in the Filters section. The keyword blacklist and the
main blacklist are both viewable and editable from the UI. Cleared blacklist entries
take effect on the next run.

### Settings

- Paste the Google Sheet ID and connect via OAuth.
- Add a webhook URL for real-time notifications.
- Choose the account switch mode (every N profiles or every N hours).
- Tune all MIN/MAX delay values.
- Configure IP rotation per device.
- Set up the Main Account slot with engagement percentages and reply templates.

---

## Building the Windows EXE

### Option A — GitHub Actions (recommended)

```bat
git init
git remote add origin https://github.com/PatrickAtef8/ADB_Appium_Qt_Tooling.git
git add .
git commit -m "release"
git push -u origin main
```

The Actions workflow builds the EXE automatically. Download from the Releases section.

### Option B — Local build on Windows

```bat
pip install pyinstaller
pyinstaller instagram_scraper.spec
:: Output: dist/InstagramScraperPro.exe
```

---

## Project structure

```
IGScraper/
├── main.py
├── requirements.txt
├── instagram_scraper.spec
├── .github/
│   └── workflows/
│       └── build.yml
├── config/
│   ├── settings.json
│   ├── blacklist.json
│   ├── blacklist_keyword.json
│   └── scraping_summary.txt
├── assets/
│   └── credentials.json
└── src/
    ├── ui/
    │   └── main_window.py
    ├── automation/
    │   ├── appium_controller.py
    │   ├── appium_manager.py
    │   ├── scraper.py
    │   ├── main_account_worker.py
    │   └── ip_rotator.py
    ├── mirror/
    │   ├── mirror_widget.py
    │   └── stream_worker.py
    ├── sheets/
    │   └── google_sheets.py
    └── utils/
        ├── config_manager.py
        ├── filters.py
        ├── blacklist.py
        └── completed.py
```

---

## Troubleshooting

**No devices found** — Run `adb devices` in a terminal. USB Debugging must be on and
the device must have trusted the PC.

**Appium session fails** — Make sure `appium` is running in a separate terminal before
starting the app.

**Instagram elements not found** — Instagram updates its UI periodically. Use Appium
Inspector to find the current resource IDs and update `scraper.py` accordingly.

**Google auth fails** — Go to Settings, click Revoke Token, then reconnect to go
through the OAuth flow again.

**scrcpy not found** — Ensure scrcpy is installed and accessible on PATH. The path
can be adjusted in `appium_controller.py` if needed.

**CMD window flashes on device detection** — This is suppressed on Windows using
`CREATE_NO_WINDOW`. If it reappears, check that the bundled `scrcpy-server.jar` in
`src/mirror/assets/` is present.

---

## Developer notes

- Account switching uses ADB UI automation on the Instagram account switcher screen.
  A per-device lock prevents ADB and Appium from contending on the UIAutomator2
  accessibility stack at the same time.
- Country inference tries the phone prefix first (3→2→1 digit), then scans the
  location string with word-boundary regex (`\b`) to avoid matching short country
  codes inside longer words.
- Location extraction tries two resource IDs (`profile_header_location_text` and
  `profile_header_business_address`) and falls back to pin-emoji lines in the bio.
- Profile picture detection takes a screenshot and pixel-samples the avatar area
  rather than relying on element presence, which is more reliable across Instagram
  versions.
- The scraping summary (`config/scraping_summary.txt`) is a human-readable log.
  Completed usernames are parsed from it at startup to skip already-finished targets.
- All delay values are randomised: `between_profiles_min/max`, `between_scrolls_min/max`,
  `rest_min/max_seconds`, `session_break_every`.
- Webhook sends and Google Sheets appends run in daemon threads so they never block
  the scraper loop.
- The mirror panel width is persisted to `settings.json` under `mirror_width` and
  restored on the next launch.