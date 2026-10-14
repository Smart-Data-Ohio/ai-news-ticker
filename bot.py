#!/usr/bin/env python3
"""Poll RSS/Atom feeds, rank AI news, and post new items to Slack."""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("AI_NEWS_CONFIG", ROOT / "config.toml"))
DB_PATH = Path(os.getenv("AI_NEWS_DB", ROOT / "state.sqlite3"))
USER_AGENT = "AI-News-Ticker/1.0 (+RSS reader; contact: admin)"
MAX_BODY = 1_500_000

AI_TERMS = (
    "artificial intelligence", "generative ai", "machine learning", " llm", "llms",
    "foundation model", "language model", "neural network", "openai", "anthropic",
    "deepmind", "gemini", "claude", "gpt-", "llama", "mistral", "xai", "nvidia",
    "ai model", "ai-powered", " ai ",
)
RELEASE_TERMS = (
    "introducing", "launch", "launches", "launched", "release", "releases", "released",
    "now available", "open weights", "open-source model", "new model", "announcing",
    "unveils", "debut", "preview", "general availability", "gpt-", "claude ", "gemini ",
    "llama ", "mistral ", "grok ", "deepseek",
)
MAJOR_TERMS = (
    "acquisition", "acquires", "acquired", "merger", "lawsuit", "regulation", "regulatory",
    "safety", "security", "benchmark", "funding", "raises $", "invests", "partnership",
    "data center", "chip", "antitrust", "policy", "copyright", "recall", "breach",
)


@dataclass
class Item:
    source: str
    title: str
    link: str
    summary: str
    published: datetime
    score: int
    category: str


def load_config(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    with path.open("rb") as stream:
        return tomllib.load(stream)


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in element.iter():
        if child is not element and local_name(child.tag) in wanted and child.text:
            return child.text.strip()
    return ""


def parse_date(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)


def parse_feed(xml_body: bytes, source: str) -> list[tuple[str, str, str, str, datetime]]:
    root = ET.fromstring(xml_body)
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    result = []
    for entry in entries:
        title = clean_text(child_text(entry, "title"))
        link = ""
        for node in entry:
            if local_name(node.tag) == "link":
                link = node.attrib.get("href", "") or (node.text or "").strip()
                if node.attrib.get("rel") in ("self", "enclosure"):
                    continue
                break
        link = urllib.parse.urljoin(source, link)
        summary = clean_text(child_text(entry, "summary", "description", "encoded", "content"))
        guid = child_text(entry, "guid", "id") or link or title
        date = child_text(entry, "published", "updated", "pubdate", "date", "issued")
        if title and link:
            result.append((guid, title, link, summary[:900], parse_date(date)))
    return result


def fetch_feed(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("feed exceeded 1.5 MB limit")
    return body


def rank(title: str, summary: str, source_is_ai_focused: bool) -> tuple[int, str]:
    text = f" {title} {summary} ".lower()
    score = 2 if source_is_ai_focused else 0
    if any(term in text for term in AI_TERMS):
        score += 2
    release = any(term in text for term in RELEASE_TERMS)
    major = any(term in text for term in MAJOR_TERMS)
    if release:
        score += 5
    if major:
        score += 3
    if "model" in text or any(term in text for term in ("gpt-", "claude", "gemini", "llama", "grok")):
        score += 2
    category = "Model release" if release and ("model" in text or any(term in text for term in ("gpt-", "claude", "gemini", "llama", "grok", "mistral"))) else "AI news"
    return score, category


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS stories (
        story_key TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, link TEXT NOT NULL,
        summary TEXT NOT NULL, published TEXT NOT NULL, score INTEGER NOT NULL,
        category TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', added_at TEXT NOT NULL,
        sent_at TEXT
    )""")
    conn.commit()
    return conn


def story_key(guid: str, link: str) -> str:
    return link.rstrip("/") if link else guid


def ingest(conn: sqlite3.Connection, config: dict, bootstrap: bool = False) -> int:
    feeds = config.get("feeds", [])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(config.get("max_age_hours", 72)))
    added = 0
    for feed in feeds:
        try:
            parsed = parse_feed(fetch_feed(feed["url"]), feed["name"])
        except Exception as exc:
            print(f"Feed unavailable: {feed.get('name', feed.get('url'))}: {exc}", file=sys.stderr)
            continue
        for guid, title, link, summary, published in parsed:
            if published < cutoff:
                continue
            score, category = rank(title, summary, bool(feed.get("ai_focused", False)))
            if score < int(config.get("minimum_score", 4)):
                continue
            now = datetime.now(timezone.utc).isoformat()
            status = "sent" if bootstrap else "pending"
            cur = conn.execute(
                "INSERT OR IGNORE INTO stories(story_key,source,title,link,summary,published,score,category,status,added_at,sent_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (story_key(guid, link), feed["name"], title, link, summary, published.isoformat(), score, category, status, now, now if bootstrap else None),
            )
            added += cur.rowcount
    conn.commit()
    return added


def fetch_pending(conn: sqlite3.Connection, limit: int) -> list[Item]:
    rows = conn.execute(
        "SELECT source,title,link,summary,published,score,category FROM stories WHERE status='pending' ORDER BY score DESC,published DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [Item(*row[:4], datetime.fromisoformat(row[4]), row[5], row[6]) for row in rows]


def slack_post(token: str, channel: str, text: str) -> None:
    payload = json.dumps({"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}).encode()
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())
    if not result.get("ok"):
        raise RuntimeError(f"Slack API error: {result.get('error', 'unknown_error')}")


def digest(items: list[Item]) -> str:
    if not items:
        return ""
    lines = [":newspaper: *AI News Ticker* — new stories"]
    for item in items:
        lines.append(f"\n• *<{item.link}|{item.title}>*\n  _{item.category} · {item.source}_")
        if item.summary:
            lines.append(f"  {item.summary[:240]}{'…' if len(item.summary) > 240 else ''}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one poll (default)")
    parser.add_argument("--watch", action="store_true", help="poll continuously at the configured interval")
    parser.add_argument("--dry-run", action="store_true", help="show pending digest without posting or marking items sent")
    parser.add_argument("--bootstrap", action="store_true", help="mark current feed items as already seen, then exit")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"Config file not found: {CONFIG_PATH}", file=sys.stderr)
        return 2
    config = load_config(CONFIG_PATH)
    conn = open_db(DB_PATH)
    if args.bootstrap:
        count = ingest(conn, config, bootstrap=True)
        print(f"Bootstrap complete: recorded {count} current stories as already seen.")
        return 0

    token = os.getenv("SLACK_BOT_TOKEN")
    channel = os.getenv("SLACK_CHANNEL_ID")
    if not args.dry_run and (not token or not channel):
        print("Set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID, or use --dry-run.", file=sys.stderr)
        return 2

    def poll() -> None:
        count = ingest(conn, config)
        items = fetch_pending(conn, int(config.get("max_items_per_post", 5)))
        if not items:
            print(f"Poll complete: no new qualifying stories ({count} added).")
            return
        message = digest(items)
        if args.dry_run:
            print(message)
            return
        slack_post(token or "", channel or "", message)
        keys = [story_key(item.link, item.link) for item in items]
        conn.executemany("UPDATE stories SET status='sent',sent_at=? WHERE story_key=?", [(datetime.now(timezone.utc).isoformat(), key) for key in keys])
        conn.commit()
        print(f"Posted {len(items)} stories to Slack ({count} added).")

    if not args.watch:
        try:
            poll()
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            print(f"Poll failed: {exc}", file=sys.stderr)
            return 1
        return 0

    interval = max(5, int(config.get("poll_interval_minutes", 15))) * 60
    print(f"Watching feeds every {interval // 60} minutes. Press Ctrl+C to stop.")
    while True:
        try:
            poll()
        except Exception as exc:
            print(f"Poll failed: {exc}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
