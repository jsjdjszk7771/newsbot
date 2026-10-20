"""Posts new items from RSS feeds to a Telegram channel as nice posts:
picture + bold title + short description + clean source link.

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

# (feed url, source name, emoji in front of the title[, keywords])
FEEDS = [
    ("https://cointelegraph.com/rss", "Cointelegraph", "⚡"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml", "CoinDesk", "🪙"),
    ("https://decrypt.co/feed", "Decrypt", "🔐"),
    ("http://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business", "📊"),
    ("https://feeds.npr.org/1017/rss.xml", "NPR Economy", "🌍"),
]

STATE_FILE = "sent.json"
MAX_PER_FEED = 1       # at most this many new posts per feed per run
MAX_PER_RUN = 2        # at most this many posts in total per run
ONLY_IMPORTANT = True  # post only big news (see IMPORTANT below)
TRANSLATE_TO = "ru"    # post language ("ru" = Russian, "" = leave English)
USE_WIKI_PHOTOS = True # try a photo of the person/company from the headline
STATE_LIMIT = 1000     # how many sent links to remember
SUMMARY_LIMIT = 120    # max length of the short description
SHOW_SUMMARY = True    # one short sentence under the headline
SHOW_SOURCE = False    # short "Source: ..." line (no link) under each post
SHOW_LINK = False      # True = add a "Read more" link under each post
CAPTION_LIMIT = 1000   # Telegram allows 1024 chars under a photo
USER_AGENT = "Mozilla/5.0 (compatible; NewsBot/1.0)"
API = "https://api.telegram.org/bot{}/{}"
BAD_IMAGE = re.compile(r"pixel|tracking|spacer|logo|undefined|1x1|\.gif", re.I)


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
        for src in re.findall(r'<img[^>]+src=["\']([^"\']+)', blob):
            if src.startswith("http") and not BAD_IMAGE.search(src):
                return src
    return None


def fetch_og_image(url):
    """Last resort: take the main picture from the article page itself."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if not r.ok:
            return None
        head = r.text[:200000]
    except Exception:
        return None
    for pat in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ):
        m = re.search(pat, head, re.I)
        if m:
            return html.unescape(m.group(1))
    return None


def is_relevant(entry, keywords):
    text = strip_html(entry.get("title")) + " " + strip_html(
        entry.get("summary") or entry.get("description")
    )
    return any(re.search(r"\b" + re.escape(k) + r"\b", text, re.I) for k in keywords)


# Big news: a headline/summary must match at least one of these to be posted
IMPORTANT = re.compile(
    r"\b(etf|sec|cftc|fed|federal reserve|ecb|rate cuts?|rate hikes?|interest rates?|"
    r"inflation|recession|gdp|unemployment|jobs report|cpi|tariffs?|sanctions?|"
    r"default|bankruptcy|bankrupt|collapse|crash(es)?|plunges?|soars?|surges?|tumbles?|"
    r"all-time high|record high|record low|"
    r"hack(ed|s)?|exploit(ed)?|stolen|ban(s|ned)?|lawsuit|sues?|charged|arrested|"
    r"approv(es|ed|al)|launch(es|ed)?|acquires?|acquisition|merger|ipo|"
    r"stablecoin|halving|liquidations?|bailout|shutdown|"
    r"billions?)\b"
    r"|\$\s?\d+(\.\d+)?\s?(b|bn|billion)\b|\$\s?\d{3,}\s?(m|million)\b"
    r"|\b\d{1,3}(\.\d+)?\s?%",
    re.I,
)

# short names -> Wikipedia article (people and organisations)
WIKI_ALIASES = {
    "trump": "Donald Trump", "biden": "Joe Biden", "putin": "Vladimir Putin",
    "musk": "Elon Musk", "powell": "Jerome Powell", "zelensky": "Volodymyr Zelenskyy",
    "xi": "Xi Jinping", "netanyahu": "Benjamin Netanyahu", "khamenei": "Ali Khamenei",
    "altman": "Sam Altman", "zuckerberg": "Mark Zuckerberg", "bezos": "Jeff Bezos",
    "nadella": "Satya Nadella", "huang": "Jensen Huang", "cook": "Tim Cook",
    "saylor": "Michael J. Saylor", "buterin": "Vitalik Buterin", "cz": "Changpeng Zhao",
    "fed": "Federal Reserve", "sec": "U.S. Securities and Exchange Commission",
    "iran": "Iran", "bitcoin": "Bitcoin", "ethereum": "Ethereum",
    "openai": "OpenAI", "nvidia": "Nvidia", "apple": "Apple Inc.",
    "google": "Google", "microsoft": "Microsoft", "meta": "Meta Platforms",
    "tesla": "Tesla, Inc.", "binance": "Binance", "coinbase": "Coinbase",
    "anthropic": "Anthropic", "telegram": "Telegram (software)", "durov": "Pavel Durov",
    "toncoin": "Toncoin", "notcoin": "Notcoin", "solana": "Solana (blockchain platform)",
}
SKIP_WORDS = {"the", "a", "an", "why", "how", "what", "when", "who", "as", "in", "on",
              "at", "to", "for", "of", "new", "just", "report", "says", "after", "will"}


def is_important(entry):
    text = strip_html(entry.get("title")) + " " + strip_html(
        entry.get("summary") or entry.get("description")
    )
    return bool(IMPORTANT.search(text))


def wiki_photo(title):
    """Find a photo of the person / company named in the headline (Wikipedia)."""
    candidates = []
    words = re.findall(r"[A-Za-z0-9.&'-]+", title)
    for w in words:  # known short names first (Trump, Musk, Fed ...)
        alias = WIKI_ALIASES.get(w.lower().strip(".'-"))
        if alias and alias not in candidates:
            candidates.append(alias)
    for m in re.findall(r"\b[A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*)+", title):  # full names
        if m.split()[0].lower() not in SKIP_WORDS and m not in candidates:
            candidates.insert(0, m)
    for w in re.findall(r"\b[A-Z][a-zA-Z0-9]{3,}\b", title)[1:]:  # single names (Bybit ...)
        if w.lower() not in SKIP_WORDS and w not in candidates:
            candidates.append(w)
    for name in candidates[:5]:
        try:
            r = requests.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                + requests.utils.quote(name.replace(" ", "_"), safe="(),._"),
                headers={"User-Agent": USER_AGENT}, timeout=10,
            )
            if not r.ok:
                continue
            data = r.json()
            if data.get("type") == "disambiguation":
                continue
            img = (data.get("originalimage") or data.get("thumbnail") or {}).get("source")
            if not img:
                continue
            thumb = (data.get("thumbnail") or {}).get("source")
            # a ~1000px version is plenty for Telegram and always small enough
            if thumb:
                img = re.sub(r"/\d+px-", "/1000px-", thumb)
            return img
        except Exception:
            continue
    return None


TAGS_RU = {"JUST IN": "СРОЧНО", "NEW": "НОВОЕ", "UPDATE": "ОБНОВЛЕНИЕ"}


def translate(text):
    """Free Google Translate endpoint; on any problem keep the original text."""
    if not TRANSLATE_TO or not text:
        return text
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": TRANSLATE_TO,
                    "dt": "t", "q": text},
            headers={"User-Agent": USER_AGENT}, timeout=15,
        )
        if not r.ok:
            return text
        out = "".join(part[0] for part in r.json()[0] if part and part[0])
        return out.strip() or text
    except Exception:
        return text


def pick_tag(title):
    t = title.lower()
    if re.search(r"\b(update|upgrade|rises?|falls?|drops?|surges?|jumps?)\b", t):
        return "🔄", "UPDATE"
    if re.search(r"\b(launch\w*|opens?|unveils?|announces?|introduces?|new|raises?)\b", t):
        return "🆕", "NEW"
    return "🚨", "JUST IN"


def build_caption(emoji, source, title, summary, link):
    icon, tag = pick_tag(title)
    if not SHOW_SUMMARY or summary.lower().startswith(title.lower()[:60]):
        summary = ""  # description just repeats the title
    if TRANSLATE_TO == "ru":
        tag = TAGS_RU.get(tag, tag)
    title = translate(title)
    summary = translate(summary)

    def make(summ):
        parts = [f"{icon} <b>{tag}:</b> {html.escape(title, quote=False)}"]
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
    posted = 0

    def remember(link):
        nonlocal changed
        if link not in sent_set:
            sent.append(link)
            sent_set.add(link)
        changed = True

    for feed_cfg in FEEDS:
        url, source, emoji = feed_cfg[:3]
        keywords = feed_cfg[3] if len(feed_cfg) > 3 else None
        feed = feedparser.parse(url, agent=USER_AGENT)
        if not feed.entries:
            print("No entries (or feed unreachable):", url, file=sys.stderr)
            continue

        # feeds list newest first
        new = [e for e in feed.entries if e.get("link") and e["link"] not in sent_set]

        if keywords:
            keep = []
            for e in new:
                if is_relevant(e, keywords):
                    keep.append(e)
                else:
                    remember(e["link"])  # not on topic: never look at it again
            new = keep

        if ONLY_IMPORTANT:
            keep = []
            for e in new:
                if is_important(e):
                    keep.append(e)
                else:
                    remember(e["link"])  # not big news: skip for good
            new = keep

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
            if posted >= MAX_PER_RUN:
                break  # enough for this run; the rest stays for the next one
            title = strip_html(e.get("title"))
            summary = clean_summary(e.get("summary") or e.get("description"))
            caption = build_caption(emoji, source, title, summary, clean_link(e["link"]))
            # only remember after a successful send, so nothing is lost on errors
            image = (wiki_photo(title) if USE_WIKI_PHOTOS else None) \
                or find_image(e) or fetch_og_image(e["link"])
            if send_post(token, channel, caption, image):
                remember(e["link"])
                posted += 1
            time.sleep(2)

    if changed or first_run:
        save_state(sent)


if __name__ == "__main__":
    main()
