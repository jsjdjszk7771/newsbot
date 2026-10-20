"""Posts new items from RSS feeds to a Telegram channel as nice posts:
picture + bold title + short description + source name.

Secrets come from environment variables:
  TG_TOKEN   - bot token from BotFather
  TG_CHANNEL - channel username like @mychannel (or numeric id for private channels)
"""
import html
import json
import os
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

# (feed url, source name, emoji in front of the title)
FEEDS = [
    ("https://cointelegraph.com/rss", "Cointelegraph", "⚡"),
    ("http://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business", "📊"),
    ("https://techcrunch.com/feed/", "TechCrunch", "💻"),
]

STATE_FILE = "sent.json"
MAX_PER_FEED = 3       # at most this many new posts per feed per run
STATE_LIMIT = 1000     # how many sent links to remember
SUMMARY_LIMIT = 280    # max length of the short description
SHOW_SOURCE = True     # short "Source: ..." line (no link) under each post
SHOW_LINK = False      # True = add a "Read more" link under each post
CAPTION_LIMIT = 1000   # Telegram allows 1024 chars under a photo
USER_AGENT = "Mozilla/5.0 (compatible; NewsBot/1.0)"
API = "https://api.telegram.org/bot{}/{}"


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(links):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(links[-STATE_LIMIT:], f, ensure_ascii=False, indent=0)


def clean_link(url):
    """Remove tracking parameters (utm_..., at_...) from a link."""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith(("utm_", "at_"))
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text, limit=SUMMARY_LIMIT):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end > limit * 0.5:
        return cut[: end + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:- ") + "…"


def clean_summary(raw):
    text = strip_html(raw)
    text = re.sub(r"\s*The post .{0,300}? appeared first on .*$", "", text)
    return shorten(text)


def find_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key) or []:
            url = m.get("url")
            kind = (m.get("type") or "") + (m.get("medium") or "")
            if url and "video" not in kind:
                return url
    for enc in entry.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image") and enc.get("href"):
            return enc["href"]
    blobs = [entry.get("summary") or ""]
    blobs += [c.get("value", "") for c in entry.get("content") or []]
    for blob in blobs:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', blob)
        if m:
            return m.group(1)
    return None


def build_caption(emoji, source, title, summary, link):
    def make(summ):
        parts = [f"{emoji} <b>{html.escape(title, quote=False)}</b>"]
        if summ:
            parts.append(html.escape(summ, quote=False))
        if SHOW_SOURCE:
            parts.append(f"<i>Source: {html.escape(source, quote=False)}</i>")
        if SHOW_LINK:
            parts.append(
                f'<a href="{html.escape(link, quote=True)}">'
                f"Read more on {html.escape(source, quote=False)}</a>"
            )
        return "\n\n".join(parts)

    if summary.lower().startswith(title.lower()[:60]):
        summary = ""  # description just repeats the title
    caption = make(summary)
    if len(caption) > CAPTION_LIMIT:
        caption = make("")
    return caption


def tg(token, method, **data):
    r = requests.post(API.format(token, method), data=data, timeout=30)
    if not r.ok:
        print(f"Telegram error ({method}):", r.status_code, r.text, file=sys.stderr)
    return r.ok


def send_post(token, channel, caption, image):
    if image and tg(
        token, "sendPhoto", chat_id=channel, photo=image,
        caption=caption, parse_mode="HTML",
    ):
        return True
    # no picture (or the picture failed): send as plain text post
    return tg(
        token, "sendMessage", chat_id=channel, text=caption,
        parse_mode="HTML", disable_web_page_preview="true",
    )


def main():
    token = os.environ.get("TG_TOKEN")
    channel = os.environ.get("TG_CHANNEL")
    if not token or not channel:
        sys.exit("TG_TOKEN and TG_CHANNEL must be set")

    state = load_state()
    first_run = state is None
    sent = state or []
    sent_set = set(sent)
    changed = False

    def remember(link):
        nonlocal changed
        if link not in sent_set:
            sent.append(link)
            sent_set.add(link)
        changed = True

    for url, source, emoji in FEEDS:
        feed = feedparser.parse(url, agent=USER_AGENT)
        if not feed.entries:
            print("No entries (or feed unreachable):", url, file=sys.stderr)
            continue

        # feeds list newest first
        new = [e for e in feed.entries if e.get("link") and e["link"] not in sent_set]

        if first_run:
            # first run: post only the latest item as a test,
            # remember the rest so old news do not flood the channel
            to_post, skipped = new[:1], new[1:]
        else:
            to_post = list(reversed(new[:MAX_PER_FEED]))  # oldest first
            skipped = new[MAX_PER_FEED:]

        for e in skipped:
            remember(e["link"])

        for e in to_post:
            title = strip_html(e.get("title"))
            summary = clean_summary(e.get("summary") or e.get("description"))
            caption = build_caption(emoji, source, title, summary, clean_link(e["link"]))
            # only remember after a successful send, so nothing is lost on errors
            if send_post(token, channel, caption, find_image(e)):
                remember(e["link"])
            time.sleep(2)

    if changed or first_run:
        save_state(sent)


if __name__ == "__main__":
    main()