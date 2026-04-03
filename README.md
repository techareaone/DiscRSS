# DiscRSS by Tradely

> A [TRADELY](https://doc.tradely.dev) Project

A standalone Python daemon that monitors RSS and Atom feeds and sends rich embed notifications to Discord channels via webhooks.  
It runs **headless** – no GUI, no display – and works on any platform where Python 3.9+ is available: Windows, macOS, Linux, Raspberry Pi, cloud VMs, etc.

> ⚠️ **Disclaimer**  
> This tool relies on third‑party RSS feeds which may change or become unavailable at any time. The authors are not responsible for feed content or availability.

---

## Overview

DiscRSS periodically polls a configurable list of RSS/Atom feeds, stores new entries in a local SQLite database (deduplicated), and dispatches rich embed notifications to one or more Discord webhooks.  
It is designed to run continuously – as a background service, a systemd unit, a Windows scheduled task, or simply inside a `screen`/`tmux` session.

### Key Features

- **Fully headless** – no GUI, no X11, no desktop environment required.
- **Cross‑platform** – Python 3.9+ with only `requests` and `feedparser` as external dependencies.
- **Multi‑feed, multi‑webhook** – monitor dozens of feeds and route each one to a different Discord channel.
- **Rich Discord embeds** – each notification includes the article title, description, link, publish date, and a footer linking to the source domain.
- **Persistent SQLite database** – keeps a complete, deduplicated history of all feed items.
- **Smart initial sync** – on first run (empty DB), only items from the last N hours are notified (avoids spam).
- **Optional logging webhook** – receive startup, success, and error notifications in a dedicated Discord channel.
- **Configurable polling interval** – set how often feeds are checked (default: every 10 minutes).
- **Conditional HTTP requests** – uses `ETag` and `If-Modified-Since` headers to avoid redundant downloads.
- **Graceful shutdown** – responds to `SIGINT`/`SIGTERM`.
- **Rotating log files** – logs to both stdout and a rotating file inside the data directory.

---

## Requirements

| Item                  | Notes                                             |
|-----------------------|---------------------------------------------------|
| Python 3.9+           | Any OS (Windows, macOS, Linux, Raspberry Pi)      |
| `requests` library    | Install via `pip install requests`                |
| `feedparser` library  | Install via `pip install feedparser`              |
| Internet connection   | To reach RSS feeds and Discord                    |
| Discord webhook URL   | Create in your Discord server's channel settings  |

---

## File Structure

The entire application lives in a single directory. All data and configuration are stored inside a subfolder `DiscRSS_Data/` next to the script.

```
/path/to/your/project/
├── discrss.py               # The main script
└── DiscRSS_Data/            # Automatically created on first run
    ├── config.env           # Configuration file (you create this)
    ├── feeds.db             # SQLite database (auto‑created)
    └── discrss.log          # Rotating log file (auto‑created)
```

No other files are required. The script creates `DiscRSS_Data/` if it does not exist.

---

## Configuration

Create a file named `config.env` inside the `DiscRSS_Data/` folder.  
Use the following template (replace the placeholder values):

```ini
# ── Required ──────────────────────────────────────────────
FEEDS=https://feeds.example.com/rss,https://pypi.org/rss/project/some-package/releases.xml,https://news.ycombinator.com/rss

DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxxxxxxx/yyyyyyyyyy

# ── Per‑Feed Webhook Routing (optional) ──────────────────
# Override the default webhook for specific feeds.
# Format: feed_url|webhook_url  (comma‑separated entries)
FEED_WEBHOOKS=https://pypi.org/rss/project/some-package/releases.xml|https://discord.com/api/webhooks/aaaa/bbbb,https://news.ycombinator.com/rss|https://discord.com/api/webhooks/cccc/dddd

# ── Feed Labels (optional) ───────────────────────────────
# Friendly names shown in the embed footer.
# Format: feed_url|label  (comma‑separated entries)
FEED_LABELS=https://pypi.org/rss/project/some-package/releases.xml|PyPI Updates,https://news.ycombinator.com/rss|Hacker News

# ── Logging Webhook (optional) ───────────────────────────
LOG_WEBHOOK_URL=https://discord.com/api/webhooks/llll/mmmm

# ── Tuning ────────────────────────────────────────────────
POLL_INTERVAL_MINUTES=10
INITIAL_NOTIFY_HOURS=24
LOG_LEVEL=INFO
DISCORD_SEND_DELAY=1.0
MAX_DISCORD_BATCH=25
REQUEST_TIMEOUT=30
```

### Configuration Reference

| Variable                | Default | Description                                                                                     |
|-------------------------|---------|-------------------------------------------------------------------------------------------------|
| `FEEDS`                 | *(required)* | Comma‑separated list of RSS or Atom feed URLs to monitor.                                  |
| `DISCORD_WEBHOOK_URL`   | *(required unless all feeds have explicit webhooks)* | Default Discord webhook URL used for any feed without a specific mapping. |
| `FEED_WEBHOOKS`         | *(empty)* | Per‑feed webhook overrides. Format: `feed_url\|webhook_url` entries, comma‑separated.        |
| `FEED_LABELS`           | *(empty)* | Friendly names for feeds shown in the embed footer. Format: `feed_url\|label`, comma‑separated. If not set, the feed's domain name is used. |
| `LOG_WEBHOOK_URL`       | *(empty)* | Optional webhook for operational log messages (startup, fetch success/failure, shutdown).     |
| `POLL_INTERVAL_MINUTES` | `10`    | How often (in minutes) all feeds are polled. Keep this reasonable to avoid rate limits.         |
| `INITIAL_NOTIFY_HOURS`  | `24`    | On first run (empty DB), only notify items published within the last N hours.                  |
| `LOG_LEVEL`             | `INFO`  | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`.                                            |
| `DISCORD_SEND_DELAY`    | `1.0`   | Seconds to wait between sending Discord embeds (rate‑limit safety).                            |
| `MAX_DISCORD_BATCH`     | `25`    | Maximum number of new items to send per feed per poll cycle.                                    |
| `REQUEST_TIMEOUT`       | `30`    | HTTP timeout in seconds for feed requests.                                                     |

---

## Webhook Routing — How It Works

DiscRSS lets you route different feeds to different Discord channels using a single running instance.

**Example scenario:** You want PyPI package updates in `#python-releases` and Hacker News in `#tech-news`.

```ini
# Both feeds listed
FEEDS=https://pypi.org/rss/project/requests/releases.xml,https://news.ycombinator.com/rss

# Default webhook (used for any feed not listed in FEED_WEBHOOKS)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/111/aaa

# Route specific feeds to specific channels
FEED_WEBHOOKS=https://pypi.org/rss/project/requests/releases.xml|https://discord.com/api/webhooks/222/bbb,https://news.ycombinator.com/rss|https://discord.com/api/webhooks/333/ccc
```

If a feed is not listed in `FEED_WEBHOOKS`, it falls back to `DISCORD_WEBHOOK_URL`.

---

## Logging Webhook

Set `LOG_WEBHOOK_URL` to receive operational notifications in a dedicated Discord channel. The following events are sent automatically:

| Event           | Level     | When it fires                                      |
|-----------------|-----------|---------------------------------------------------|
| **Feed Started** | ℹ️ INFO    | When DiscRSS starts up, listing all monitored feeds |
| **Fetch Success**| ✅ SUCCESS | After a feed poll finds new items                  |
| **Fetch Failed** | ❌ ERROR   | When a feed cannot be reached or parsed             |
| **Shutdown**     | ⚠️ WARNING | When DiscRSS stops (graceful shutdown)              |

This is entirely optional. If `LOG_WEBHOOK_URL` is not set, no log messages are sent to Discord (they still appear in the local log file).

---

## Running the Script

### Basic Execution (for testing)

```bash
cd /path/to/your/project
pip install requests feedparser
python3 discrss.py
```

Press `Ctrl+C` to stop. The script logs to the console and to `DiscRSS_Data/discrss.log`.

### Running as a Background Process

- **Linux / macOS** – use `screen`, `tmux`, or a systemd service (see below).
- **Windows** – use Task Scheduler or `pythonw.exe`.

---

## How It Works

1. **Startup** – reads `config.env`, initialises SQLite, creates tables if missing.
2. **Polling** – fetches each configured RSS/Atom feed using conditional HTTP requests (`ETag`, `If-Modified-Since`).
3. **Parsing** – extracts entries via `feedparser`, strips HTML from descriptions, and creates `FeedItem` objects.
4. **Deduplication** – inserts only new items using a SHA‑256 key based on feed URL, item link, and title.
5. **Notification** – for each new item, sends a Discord embed to the mapped webhook. On first run (empty DB), only items within `INITIAL_NOTIFY_HOURS` are sent.
6. **Logging** – if `LOG_WEBHOOK_URL` is set, sends success/failure summaries after each poll.
7. **Sleep** – waits `POLL_INTERVAL_MINUTES`, checking for shutdown every second.
8. **Loop** – repeats forever.

---

## Discord Embed Format

Each notification is sent as a rich embed containing:

- **Title** — the article/item title (clickable link to the original article).
- **Description** — a truncated plain‑text summary of the article content (up to 350 characters).
- **Published** — the item's publication date and time (UTC).
- **Footer** — the feed label (or domain) and the source domain, e.g. `📡 Hacker News  •  news.ycombinator.com`.
- **Colour** — a deterministic colour per feed so items from the same source are visually grouped.

---

## Raspberry Pi / Systemd Setup (Recommended for 24/7 Operation)

### 1. Prepare the environment

```bash
mkdir -p /home/tradely/discrss
cd /home/tradely/discrss

# Install system packages
sudo apt update
sudo apt install python3-venv -y

# Create a virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install requests feedparser
deactivate

# Create the data/config folder
mkdir -p DiscRSS_Data

# Create your config.env file (edit with your actual values)
cat > DiscRSS_Data/config.env << 'EOF'
FEEDS=https://news.ycombinator.com/rss
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK
POLL_INTERVAL_MINUTES=10
LOG_LEVEL=INFO
EOF

# Make the script executable
chmod +x discrss.py

# Test run (Ctrl+C to stop)
source venv/bin/activate
python discrss.py
```

### 2. Create the systemd service

```bash
sudo bash -c 'cat > /etc/systemd/system/discrss.service << EOF
[Unit]
Description=DiscRSS — RSS to Discord Daemon
After=network.target

[Service]
User=tradely
WorkingDirectory=/home/tradely/discrss
ExecStart=/home/tradely/discrss/venv/bin/python /home/tradely/discrss/discrss.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF'
```

### 3. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable discrss
sudo systemctl start discrss
```

### 4. Check status and logs

```bash
sudo systemctl status discrss
journalctl -u discrss -f
tail -f /home/tradely/discrss/DiscRSS_Data/discrss.log
```

### 5. Stopping / restarting

```bash
sudo systemctl stop discrss
sudo systemctl restart discrss
```

---

## Logging

Two logging destinations are used simultaneously:

- **stdout** → captured by `journalctl` when running as a systemd service.
- **Rotating file** → `DiscRSS_Data/discrss.log` (max 5 MB, 3 backups).

Example log output:

```
2025-06-15 10:00:01 [INFO] discrss: DiscRSS started. Monitoring 3 feed(s), polling every 10 minute(s).
2025-06-15 10:00:01 [INFO] discrss:   Feed: Hacker News → https://discord.com/api/webhooks/333/c...
2025-06-15 10:00:03 [INFO] discrss: Feed [Hacker News] — found:30 new:5 notified:5 (1.2s)
2025-06-15 10:00:04 [INFO] discrss: Feed [PyPI Updates] — found:3 new:1 notified:1 (0.8s)
2025-06-15 10:00:04 [INFO] discrss: Next poll in 10 minute(s).
```

---

## Database Schema

The file `feeds.db` (inside `DiscRSS_Data/`) contains a table `items`:

| Column        | Type    | Description                                  |
|---------------|---------|----------------------------------------------|
| `id`          | INTEGER | Auto‑increment primary key                  |
| `dedupe_key`  | TEXT    | SHA‑256 hash (unique)                       |
| `title`       | TEXT    | Article / item title                        |
| `description` | TEXT    | Plain‑text summary (truncated)              |
| `link`        | TEXT    | URL to the original article                 |
| `published`   | TEXT    | ISO datetime (UTC) when the item was published |
| `feed_url`    | TEXT    | The RSS feed URL this item came from        |
| `feed_domain` | TEXT    | Extracted domain of the feed                |
| `feed_label`  | TEXT    | Friendly label (from config or domain)      |
| `fetched_at`  | TEXT    | UTC timestamp when this record was inserted |

There is also a `schema_version` table storing the current schema version (integer).

---

## Troubleshooting

| Symptom                                            | Likely fix                                                                  |
|----------------------------------------------------|-----------------------------------------------------------------------------|
| `FATAL: FEEDS is not set`                          | Check that `config.env` exists in `DiscRSS_Data/` and contains `FEEDS=`.   |
| `FATAL: No Discord webhook configured`             | Set `DISCORD_WEBHOOK_URL` or `FEED_WEBHOOKS` in `config.env`.              |
| `ModuleNotFoundError: No module named 'feedparser'` | Run `pip install feedparser` (inside your venv if using one).              |
| `ModuleNotFoundError: No module named 'requests'`   | Run `pip install requests`.                                                |
| No Discord notifications                            | Verify webhook URL, check Discord server settings, look for errors in logs.|
| Duplicate notifications after restart                | This should not happen — the SQLite DB persists between restarts.          |
| Too many notifications on first start                | Lower `INITIAL_NOTIFY_HOURS` (default 24) to reduce the lookback window.  |
| Rate limit errors (HTTP 429)                         | Increase `POLL_INTERVAL_MINUTES` or `DISCORD_SEND_DELAY`.                 |

---

## Updating the Script

```bash
cd /home/tradely/discrss
sudo systemctl stop discrss
# Replace discrss.py with the new version
sudo systemctl start discrss
```

The database and `config.env` remain untouched.

---

## Uninstalling

```bash
sudo systemctl stop discrss
sudo systemctl disable discrss
sudo rm /etc/systemd/system/discrss.service
sudo systemctl daemon-reload
rm -rf /home/tradely/discrss
```

---

## License

© TRADELY.DEV. All rights reserved. Refer to the repository licence file for terms.

---

## Final Notes

- The script only connects to your configured RSS feeds and Discord — no telemetry, no auto‑updater.
- For security, consider running the service under a dedicated user with limited permissions.
- To debug interactively, run `python discrss.py` from the terminal — log output will appear directly.
- Feed labels make your embeds much more readable — consider setting `FEED_LABELS` for all your feeds.

**Happy monitoring!** 📡🔔
