#!/usr/bin/env python3
"""
DiscRSS — RSS-to-Discord Notification Daemon
A headless Python daemon that monitors RSS/Atom feeds and sends
rich embed notifications to Discord webhooks.

pip install requests feedparser

No display or GUI required. Configuration is read from DiscRSS_Data/config.env.
"""
from __future__ import annotations
import hashlib, logging, os, signal, sqlite3, sys, threading, time, re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any, Optional, Sequence
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    sys.exit("FATAL: pip install requests")

try:
    import feedparser
except ImportError:
    sys.exit("FATAL: pip install feedparser")

# ============================================================
#  PATHS & CONSTANTS
# ============================================================
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(SCRIPT_DIR, "DiscRSS_Data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.env")
DB_PATH     = os.path.join(DATA_DIR, "feeds.db")
os.makedirs(DATA_DIR, exist_ok=True)

SCHEMA_VERSION = 1
USER_AGENT     = "DiscRSS/1.0 (Python; +https://github.com/tradely/discrss)"
logger         = logging.getLogger("discrss")
_shutdown      = False


def _sig(s, f):
    global _shutdown
    _shutdown = True


# ============================================================
#  CONFIG FILE HELPERS
# ============================================================
def _read_env(path: str) -> dict:
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if k:
                out[k] = v
    return out


def _write_env(path: str, updates: dict) -> None:
    existing = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as fh:
            existing = fh.readlines()
    written = set()
    new_lines = []
    for line in existing:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}\n")
                written.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in written:
            new_lines.append(f"{k}={v}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(new_lines)


def load_config_env() -> dict:
    data = _read_env(CONFIG_PATH)
    for k, v in data.items():
        if k not in os.environ:
            os.environ[k] = v
    return data


# ============================================================
#  CONFIGURATION
# ============================================================
@dataclass
class FeedMapping:
    """Maps a single RSS feed URL to a Discord webhook URL."""
    feed_url: str
    webhook_url: str
    label: str = ""  # optional friendly name

    @property
    def domain(self) -> str:
        parsed = urlparse(self.feed_url)
        return parsed.netloc or parsed.path.split("/")[0]


@dataclass
class Config:
    feed_mappings: list[FeedMapping]
    default_webhook_url: str
    poll_interval_minutes: int = 10
    discord_send_delay: float = 1.0
    max_discord_batch: int = 25
    request_timeout: int = 30
    initial_notify_hours: int = 24
    log_level: str = "INFO"
    log_webhook_url: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        default_webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        log_webhook = os.getenv("LOG_WEBHOOK_URL", "").strip()

        # Parse FEEDS: comma-separated list of RSS URLs
        feeds_raw = os.getenv("FEEDS", "").strip()
        feed_urls = [u.strip() for u in feeds_raw.split(",") if u.strip()] if feeds_raw else []

        # Parse FEED_WEBHOOKS: pipe-separated mappings of feed_url|webhook_url
        # If a feed is not mapped here, it uses the default webhook
        feed_webhooks_raw = os.getenv("FEED_WEBHOOKS", "").strip()
        webhook_map: dict[str, str] = {}
        if feed_webhooks_raw:
            for entry in feed_webhooks_raw.split(","):
                entry = entry.strip()
                if "|" in entry:
                    parts = entry.split("|", 1)
                    feed_key = parts[0].strip()
                    wh_url = parts[1].strip()
                    webhook_map[feed_key] = wh_url

        # Parse FEED_LABELS: pipe-separated mappings of feed_url|label
        feed_labels_raw = os.getenv("FEED_LABELS", "").strip()
        label_map: dict[str, str] = {}
        if feed_labels_raw:
            for entry in feed_labels_raw.split(","):
                entry = entry.strip()
                if "|" in entry:
                    parts = entry.split("|", 1)
                    feed_key = parts[0].strip()
                    lbl = parts[1].strip()
                    label_map[feed_key] = lbl

        # Build feed mappings
        mappings = []
        for url in feed_urls:
            wh = webhook_map.get(url, default_webhook)
            lbl = label_map.get(url, "")
            if not wh:
                logger.warning("No webhook for feed %s and no default set — skipping.", url)
                continue
            mappings.append(FeedMapping(feed_url=url, webhook_url=wh, label=lbl))

        if not mappings and not feed_urls:
            logger.error("FATAL: No feeds configured. Set FEEDS in config.env.")

        return cls(
            feed_mappings=mappings,
            default_webhook_url=default_webhook,
            poll_interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", "10")),
            discord_send_delay=float(os.getenv("DISCORD_SEND_DELAY", "1.0")),
            max_discord_batch=int(os.getenv("MAX_DISCORD_BATCH", "25")),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
            initial_notify_hours=int(os.getenv("INITIAL_NOTIFY_HOURS", "24")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_webhook_url=log_webhook,
        )


# ============================================================
#  HTML / TEXT HELPERS
# ============================================================
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Remove HTML tags and decode entities, then collapse whitespace."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate(text: str, limit: int = 300) -> str:
    """Truncate text to a maximum length, adding ellipsis if needed."""
    if not text or len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


# ============================================================
#  FEED ITEM MODEL
# ============================================================
@dataclass
class FeedItem:
    title: str
    description: str
    link: str
    published: Optional[datetime]
    feed_url: str
    feed_domain: str
    feed_label: str

    @property
    def dedupe_key(self) -> str:
        raw = "|".join([self.feed_url, self.link or "", self.title or ""])
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_entry(cls, entry: dict, mapping: FeedMapping) -> Optional["FeedItem"]:
        title = strip_html(entry.get("title", "")).strip()
        link = entry.get("link", "").strip()
        if not title and not link:
            return None

        # Extract description from various fields
        desc_raw = ""
        if "summary" in entry:
            desc_raw = entry["summary"]
        elif "description" in entry:
            desc_raw = entry["description"]
        elif "content" in entry and entry["content"]:
            desc_raw = entry["content"][0].get("value", "")
        description = truncate(strip_html(desc_raw), 350)

        # Parse published date
        published = None
        pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub_parsed:
            try:
                from calendar import timegm
                published = datetime.fromtimestamp(timegm(pub_parsed), tz=timezone.utc)
            except Exception:
                pass

        return cls(
            title=title or "(Untitled)",
            description=description,
            link=link,
            published=published,
            feed_url=mapping.feed_url,
            feed_domain=mapping.domain,
            feed_label=mapping.label or mapping.domain,
        )


# ============================================================
#  SQLITE STORE
# ============================================================
class FeedStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        description TEXT,
        link TEXT,
        published TEXT,
        feed_url TEXT NOT NULL,
        feed_domain TEXT,
        feed_label TEXT,
        fetched_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_fetched ON items (fetched_at DESC);
    CREATE INDEX IF NOT EXISTS idx_feed ON items (feed_url);
    """

    def __init__(self, path: str):
        self._path = path
        self._c = sqlite3.connect(self._path, check_same_thread=False)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        return self._c

    def close(self):
        self._c.close()

    def init(self):
        with self._lock:
            c = self._conn()
            c.executescript(self.DDL)
            c.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            c.commit()

    def count(self) -> int:
        with self._lock:
            r = self._conn().execute("SELECT COUNT(*) FROM items").fetchone()
            return r[0] if r else 0

    def insert_new(self, items: Sequence[FeedItem]) -> list[FeedItem]:
        if not items:
            return []
        with self._lock:
            conn = self._conn()
            now = datetime.now(timezone.utc).isoformat()
            keys = [it.dedupe_key for it in items]
            existing = set()
            for i in range(0, len(keys), 900):
                b = keys[i : i + 900]
                existing.update(
                    r[0]
                    for r in conn.execute(
                        f"SELECT dedupe_key FROM items WHERE dedupe_key IN ({','.join('?' * len(b))})",
                        b,
                    )
                )
            new, rows = [], []
            for it in items:
                if it.dedupe_key in existing:
                    continue
                existing.add(it.dedupe_key)
                new.append(it)
                rows.append(
                    (
                        it.dedupe_key,
                        it.title,
                        it.description,
                        it.link,
                        it.published.isoformat() if it.published else None,
                        it.feed_url,
                        it.feed_domain,
                        it.feed_label,
                        now,
                    )
                )
            if rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO items "
                    "(dedupe_key,title,description,link,published,feed_url,feed_domain,feed_label,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
            return new


# ============================================================
#  DISCORD NOTIFIER
# ============================================================
# Embed colour palette
EMBED_COLORS = [
    0x5865F2,  # Discord blurple
    0x57F287,  # Green
    0xFEE75C,  # Yellow
    0xEB459E,  # Pink
    0xED4245,  # Red
    0x3498DB,  # Blue
    0xE67E22,  # Orange
    0x9B59B6,  # Purple
]


def _color_for_feed(feed_url: str) -> int:
    """Deterministic colour based on feed URL hash."""
    h = int(hashlib.md5(feed_url.encode()).hexdigest(), 16)
    return EMBED_COLORS[h % len(EMBED_COLORS)]


class DiscordSender:
    def __init__(self, delay: float = 1.0, retries: int = 4, timeout: int = 20):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = USER_AGENT

    def _build_embed(self, item: FeedItem) -> dict:
        color = _color_for_feed(item.feed_url)
        embed: dict[str, Any] = {
            "title": truncate(item.title, 256),
            "color": color,
            "footer": {
                "text": f"📡 {item.feed_label}  •  {item.feed_domain}",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if item.link:
            embed["url"] = item.link
        if item.description:
            embed["description"] = truncate(item.description, 350)
        if item.published:
            embed["fields"] = [
                {
                    "name": "Published",
                    "value": item.published.strftime("%Y-%m-%d %H:%M UTC"),
                    "inline": True,
                }
            ]
        return embed

    def send(self, webhook_url: str, item: FeedItem) -> bool:
        payload = {"embeds": [self._build_embed(item)]}
        for attempt in range(self.retries):
            try:
                r = self._s.post(webhook_url, json=payload, timeout=self.timeout)
            except Exception as e:
                logger.debug("Discord send error: %s", e)
                return False
            if r.status_code in (200, 204):
                return True
            if r.status_code == 429:
                retry_after = max(1.0, float(r.headers.get("Retry-After", "2")))
                logger.debug("Rate limited, waiting %.1fs", retry_after)
                time.sleep(retry_after)
                continue
            logger.warning("Discord HTTP %d for %s", r.status_code, item.title[:60])
            return False
        return False

    def send_batch(
        self, webhook_url: str, items: Sequence[FeedItem], limit: int = 25
    ) -> int:
        sent = 0
        to_send = sorted(
            items, key=lambda x: (x.published or datetime.min.replace(tzinfo=timezone.utc))
        )[:limit]
        for item in to_send:
            if _shutdown:
                break
            if self.send(webhook_url, item):
                sent += 1
            if self.delay > 0:
                time.sleep(self.delay)
        return sent

    def send_log(self, webhook_url: str, message: str, level: str = "INFO") -> None:
        """Send a logging/status message to the logging webhook."""
        if not webhook_url:
            return
        color_map = {
            "INFO": 0x3498DB,
            "SUCCESS": 0x57F287,
            "WARNING": 0xFEE75C,
            "ERROR": 0xED4245,
        }
        icon_map = {
            "INFO": "ℹ️",
            "SUCCESS": "✅",
            "WARNING": "⚠️",
            "ERROR": "❌",
        }
        embed = {
            "title": f"{icon_map.get(level, 'ℹ️')} DiscRSS — {level}",
            "description": message,
            "color": color_map.get(level, 0x3498DB),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "DiscRSS Log"},
        }
        try:
            self._s.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        except Exception:
            pass  # logging failures should not crash the daemon


# ============================================================
#  RSS FETCHER
# ============================================================
class RSSFetcher:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = USER_AGENT
        self._etags: dict[str, str] = {}
        self._modified: dict[str, str] = {}

    def fetch(self, mapping: FeedMapping) -> list[FeedItem]:
        url = mapping.feed_url
        headers: dict[str, str] = {}
        if url in self._etags:
            headers["If-None-Match"] = self._etags[url]
        if url in self._modified:
            headers["If-Modified-Since"] = self._modified[url]

        try:
            resp = self._s.get(url, headers=headers, timeout=self.timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ConnectionError(f"Failed to reach {url}: {e}") from e

        if resp.status_code == 304:
            return []
        if resp.status_code != 200:
            raise ConnectionError(f"HTTP {resp.status_code} from {url}")

        # Cache conditional headers
        if "ETag" in resp.headers:
            self._etags[url] = resp.headers["ETag"]
        if "Last-Modified" in resp.headers:
            self._modified[url] = resp.headers["Last-Modified"]

        feed = feedparser.parse(resp.content)
        items = []
        for entry in feed.entries:
            item = FeedItem.from_entry(entry, mapping)
            if item:
                items.append(item)
        return items


# ============================================================
#  POLL CYCLE
# ============================================================
@dataclass
class PollResult:
    feed_url: str = ""
    feed_label: str = ""
    entries_found: int = 0
    new_items: int = 0
    notified: int = 0
    duration_s: float = 0.0
    error: str = ""


def poll_single_feed(
    mapping: FeedMapping,
    fetcher: RSSFetcher,
    store: FeedStore,
    discord: DiscordSender,
    cfg: Config,
    db_was_empty: bool,
) -> PollResult:
    result = PollResult(feed_url=mapping.feed_url, feed_label=mapping.label or mapping.domain)
    t0 = time.monotonic()

    try:
        items = fetcher.fetch(mapping)
    except Exception as e:
        result.error = str(e)
        result.duration_s = time.monotonic() - t0
        return result

    result.entries_found = len(items)

    try:
        new = store.insert_new(items)
    except Exception as e:
        result.error = f"DB: {e}"
        result.duration_s = time.monotonic() - t0
        return result

    result.new_items = len(new)

    if new:
        if db_was_empty:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.initial_notify_hours)
            notify = [it for it in new if it.published and it.published >= cutoff]
            logger.info(
                "Initial sync for %s: stored %d, notifying %d within last %d hours.",
                result.feed_label, len(new), len(notify), cfg.initial_notify_hours,
            )
        else:
            notify = new

        result.notified = discord.send_batch(mapping.webhook_url, notify, cfg.max_discord_batch)

    result.duration_s = time.monotonic() - t0
    return result


def poll_all_feeds(
    cfg: Config,
    fetcher: RSSFetcher,
    store: FeedStore,
    discord: DiscordSender,
) -> list[PollResult]:
    db_was_empty = store.count() == 0
    results = []
    for mapping in cfg.feed_mappings:
        if _shutdown:
            break
        r = poll_single_feed(mapping, fetcher, store, discord, cfg, db_was_empty)
        results.append(r)
        logger.info(
            "Feed [%s] — found:%d new:%d notified:%d (%.1fs)%s",
            r.feed_label,
            r.entries_found,
            r.new_items,
            r.notified,
            r.duration_s,
            f"  ERROR: {r.error}" if r.error else "",
        )
    return results


# ============================================================
#  MAIN LOOP
# ============================================================
def run_daemon(cfg: Config) -> int:
    store = FeedStore(DB_PATH)
    store.init()
    fetcher = RSSFetcher(cfg.request_timeout)
    discord = DiscordSender(cfg.discord_send_delay)

    logger.info(
        "DiscRSS started. Monitoring %d feed(s), polling every %d minute(s).",
        len(cfg.feed_mappings),
        cfg.poll_interval_minutes,
    )
    for m in cfg.feed_mappings:
        logger.info("  Feed: %s → %s", m.label or m.domain, m.webhook_url[:60] + "...")

    # Send startup log
    feed_list = ", ".join(m.label or m.domain for m in cfg.feed_mappings)
    discord.send_log(
        cfg.log_webhook_url,
        f"**DiscRSS started.**\n"
        f"Monitoring **{len(cfg.feed_mappings)}** feed(s): {feed_list}\n"
        f"Poll interval: **{cfg.poll_interval_minutes}** minute(s)",
        "INFO",
    )

    try:
        while not _shutdown:
            # Poll all feeds
            results = poll_all_feeds(cfg, fetcher, store, discord)

            # Send per-feed log summaries
            for r in results:
                if r.error:
                    discord.send_log(
                        cfg.log_webhook_url,
                        f"**Fetch failed** for `{r.feed_label}`\n`{r.error}`",
                        "ERROR",
                    )
                elif r.new_items > 0:
                    discord.send_log(
                        cfg.log_webhook_url,
                        f"**Fetch success** for `{r.feed_label}` — "
                        f"{r.entries_found} entries, {r.new_items} new, "
                        f"{r.notified} notified ({r.duration_s:.1f}s)",
                        "SUCCESS",
                    )

            # Sleep until next poll
            sleep_secs = cfg.poll_interval_minutes * 60
            logger.info("Next poll in %d minute(s).", cfg.poll_interval_minutes)
            for _ in range(sleep_secs):
                if _shutdown:
                    break
                time.sleep(1)
    finally:
        store.close()
        discord.send_log(cfg.log_webhook_url, "**DiscRSS stopped.**", "WARNING")
        logger.info("DiscRSS shutdown complete.")
    return 0


# ============================================================
#  MAIN
# ============================================================
def main() -> int:
    log_file = os.path.join(DATA_DIR, "discrss.log")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        from logging.handlers import RotatingFileHandler
        handlers.append(
            RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        )
    except Exception:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    load_config_env()

    # Validate
    feeds_raw = os.getenv("FEEDS", "").strip()
    if not feeds_raw:
        logger.error("FATAL: FEEDS is not set in %s", CONFIG_PATH)
        logger.error("Add at least one RSS feed URL: FEEDS=https://example.com/rss")
        return 1

    default_wh = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    feed_wh = os.getenv("FEED_WEBHOOKS", "").strip()
    if not default_wh and not feed_wh:
        logger.error("FATAL: No Discord webhook configured in %s", CONFIG_PATH)
        logger.error(
            "Set DISCORD_WEBHOOK_URL for a default, or FEED_WEBHOOKS for per-feed routing."
        )
        return 1

    cfg = Config.from_env()
    logging.getLogger("discrss").setLevel(cfg.log_level)

    if not cfg.feed_mappings:
        logger.error("FATAL: No valid feed mappings could be built. Check config.env.")
        return 1

    logger.info("Starting DiscRSS")
    logger.info("Data directory : %s", DATA_DIR)
    logger.info("Database       : %s", DB_PATH)
    logger.info("Poll interval  : %d minute(s)", cfg.poll_interval_minutes)
    return run_daemon(cfg)


if __name__ == "__main__":
    sys.exit(main())
