"""News feed + sentiment — RSS headlines filtered by symbol, scored bullish/bearish.

Two sentiment engines:
  - Rule-based keyword scorer (always available, deterministic, zero external calls).
  - Claude API scorer, used automatically when ANTHROPIC_API_KEY is set in .env —
    scores the whole headline batch in one call and falls back to keywords for
    any headline it doesn't return a classification for (or if the call fails).

RSS feed URLs are public but can move over time — if a feed 404s or changes
shape it's skipped silently (logged at debug), same defensive posture as
scrip_master's CSV loader (unknown/broken source shouldn't crash startup).
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import httpx

from backend.config import settings

log = logging.getLogger("news")

RSS_FEEDS = [
    {"name": "Economic Times Markets",
     "url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"},
    {"name": "Moneycontrol", "url": "https://www.moneycontrol.com/rss/latestnews.xml"},
    {"name": "Moneycontrol Business", "url": "https://www.moneycontrol.com/rss/business.xml"},
]

_BULLISH_WORDS = (
    "surge", "surges", "rally", "rallies", "jump", "jumps", "soar", "soars", "gain", "gains",
    "beats estimates", "beat estimates", "record high", "upgrade", "upgraded", "outperform",
    "buy rating", "profit rises", "profit jumps", "expands", "wins order",
    "raises guidance", "bullish",
)
_BEARISH_WORDS = (
    "slump", "slumps", "plunge", "plunges", "crash", "crashes", "falls", "falling",
    "tumble", "tumbles", "downgrade", "downgraded", "underperform", "sell rating",
    "misses estimates", "miss estimates", "loss widens", "profit falls", "profit drops",
    "cuts guidance", "probe", "fraud", "bearish", "selloff", "sell-off",
)


def _keyword_sentiment(text: str) -> str:
    t = text.lower()
    bull = sum(1 for w in _BULLISH_WORDS if w in t)
    bear = sum(1 for w in _BEARISH_WORDS if w in t)
    if bull > bear:
        return "BULLISH"
    if bear > bull:
        return "BEARISH"
    return "NEUTRAL"


def _claude_sentiment(headlines: list[str]) -> dict[str, str] | None:
    """Batch-score headlines with the Claude API. None -> caller falls back
    to the keyword scorer (no key set, package missing, or the call failed)."""
    if not settings.anthropic_api_key or not headlines:
        return None
    try:
        import anthropic
    except ImportError:
        log.info("anthropic package not installed — using keyword sentiment only")
        return None
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        numbered = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(headlines))
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": (
                    "Classify each numbered Indian-markets news headline as BULLISH, "
                    "BEARISH, or NEUTRAL for the stock/index it's about. Reply with "
                    "ONLY the number and classification, one per line, e.g. '1. BULLISH'."
                    "\n\n" + numbered
                ),
            }],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        out: dict[str, str] = {}
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\.\s*(BULLISH|BEARISH|NEUTRAL)", line.strip(), re.I)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(headlines):
                    out[headlines[idx]] = m.group(2).upper()
        return out or None
    except Exception as e:
        log.warning("Claude sentiment scoring failed, falling back to keywords: %s", e)
        return None


def _fetch_feed(url: str) -> list[dict]:
    try:
        resp = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        log.debug("Feed fetch/parse failed for %s: %s", url, e)
        return []
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "link": link, "pub_date": pub})
    return items


def fetch(symbol: str | None = None, limit: int = 20, use_claude: bool = True) -> list[dict]:
    """Recent headlines across configured feeds, optionally filtered to those
    mentioning `symbol` (substring match on root symbol / company name —
    naive but effective for well-known Indian large caps), each scored."""
    all_items: list[dict] = []
    for feed in RSS_FEEDS:
        for item in _fetch_feed(feed["url"]):
            item["source"] = feed["name"]
            all_items.append(item)

    if symbol:
        needle = symbol.upper()
        all_items = [it for it in all_items if needle in it["title"].upper()]

    all_items = all_items[:limit]

    claude_scores: dict[str, str] = {}
    if use_claude and all_items:
        result = _claude_sentiment([it["title"] for it in all_items])
        if result:
            claude_scores = result

    for it in all_items:
        if it["title"] in claude_scores:
            it["sentiment"] = claude_scores[it["title"]]
            it["sentiment_source"] = "claude"
        else:
            it["sentiment"] = _keyword_sentiment(it["title"])
            it["sentiment_source"] = "keyword"

    return all_items


def news_bias(items: list[dict]) -> dict:
    """Aggregate vote for the confluence engine: +1 bullish / -1 bearish / 0."""
    if not items:
        return {"vote": 0, "bullish": 0, "bearish": 0, "neutral": 0}
    bull = sum(1 for it in items if it["sentiment"] == "BULLISH")
    bear = sum(1 for it in items if it["sentiment"] == "BEARISH")
    neu = len(items) - bull - bear
    vote = 1 if bull > bear else -1 if bear > bull else 0
    return {"vote": vote, "bullish": bull, "bearish": bear, "neutral": neu}
