"""Posts new items from RSS feeds to a Telegram channel.

Secrets come from environment variables:
  TG_TOKEN   - bot token from BotFather
  TG_CHANNEL - channel username like @mychannel (or numeric id for private channels)
"""
import json
import os
import sys

import feedparser
import requests

FEEDS = [
    "https://cointelegraph.com/rss",
    "http://feeds.bbci.co.uk/news/business/rss.xml",
    "https://techcrunch.com/feed/",
]

STATE_FILE = "sent.json"
MAX_PER_FEED = 5      # at most this many new posts per feed per run
STATE_LIMIT = 1000    # how many sent links to remember
USER_AGENT = "Mozilla/5.0 (compatible; NewsBot/1.0)"


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(links):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(links[-STATE_LIMIT:], f, ensure_ascii=False, indent=0)


def send(token, channel, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": channel, "text": text},
        timeout=30,
    )
    if not r.ok:
        print("Telegram error:", r.status_code, r.text, file=sys.stderr)
    return r.ok


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

    for url in FEEDS:
        feed = feedparser.parse(url, agent=USER_AGENT)
        if not feed.entries:
            print("No entries (or feed unreachable):", url, file=sys.stderr)
            continue

        # feeds list newest first; we post oldest first
        new = [e for e in feed.entries if e.get("link") and e.link not in sent_set]

        if first_run:
            # first run: post only the latest item of each feed as a test,
            # remember everything else so old news do not flood the channel
            to_post = new[:1]
            for e in new:
                sent.append(e.link)
                sent_set.add(e.link)
            changed = True
        else:
            to_post = list(reversed(new[:MAX_PER_FEED]))
            skipped = new[MAX_PER_FEED:]
            for e in skipped:
                sent.append(e.link)
                sent_set.add(e.link)
                changed = True

        for e in to_post:
            title = (e.get("title") or "").strip()
            if send(token, channel, f"{title}\n{e.link}"):
                if e.link not in sent_set:
                    sent.append(e.link)
                    sent_set.add(e.link)
                changed = True

    if changed or first_run:
        save_state(sent)


if __name__ == "__main__":
    main()
