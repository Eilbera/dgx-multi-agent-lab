#!/usr/bin/env python3
"""Daily AI news briefing with added official feature-release monitoring."""

import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import html
import json
import re
import sys

USER_AGENT = "hermes-ai-researcher/1.2 (+contact: user@example.com)"
LOOKBACK_DAYS = 2
REDDIT_MIN_SCORE = 25

# HN: drop items whose link domain is dominated by tabloid / non-tech coverage.
HN_DOMAIN_BLACKLIST = {
    "nypost.com", "dailymail.co.uk", "thesun.co.uk", "pagesix.com",
    "foxnews.com", "breitbart.com", "tmz.com", "mirror.co.uk",
}

# Original broad AI feed plus dedicated official feature-release sources.
SOURCES = [
    # Original primary blogs
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml", "type": "rss", "max": 5},
    {"name": "DeepMind", "url": "https://deepmind.google/blog/rss.xml", "type": "rss", "max": 3},
    {"name": "Hugging Face", "url": "https://huggingface.co/blog/feed.xml", "type": "rss", "max": 3},
    {"name": "Simon Willison", "url": "https://simonwillison.net/atom/everything/", "type": "rss", "max": 3,
     "drop_patterns": ["quoting "]},
    {"name": "J. Barrow AI", "url": "https://jbarrow.ai/feed", "type": "rss", "max": 3},

    # Original community sources
    {"name": "r/LocalLLaMA", "url": "https://www.reddit.com/r/LocalLLaMA/hot.json?limit=25", "type": "reddit_json", "max": 3},
    {"name": "r/MachineLearning", "url": "https://www.reddit.com/r/MachineLearning/hot.json?limit=25", "type": "reddit_json", "max": 3,
     "drop_patterns": ["[d] self-promotion", "[d] monthly who", "[n] monthly who"]},
    {"name": "Hacker News (AI)", "url": "https://hnrss.org/newest?q=AI+OR+GPT+OR+LLM+OR+Claude+OR+Anthropic+OR+OpenAI&points=30&count=30", "type": "rss", "max": 3,
     "domain_blacklist": HN_DOMAIN_BLACKLIST},

    # Original secondary news sources
    {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed/", "type": "rss", "max": 2,
     "drop_patterns": ["3 things", "the download:"]},
    {"name": "VentureBeat AI", "url": "https://venturebeat.com/category/ai/feed/", "type": "rss", "max": 2},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "type": "rss", "max": 2},
    {"name": "Wired AI", "url": "https://www.wired.com/feed/tag/ai/latest/rss", "type": "rss", "max": 2,
     "drop_patterns": ["livestream"]},

    # Original research feeds
    {"name": "ArXiv cs.AI", "url": "https://arxiv.org/rss/cs.AI", "type": "rss", "max": 2},
    {"name": "ArXiv cs.CL", "url": "https://arxiv.org/rss/cs.CL", "type": "rss", "max": 2},

    # Added official feature-release monitoring
    {"name": "Anthropic", "url": "https://www.anthropic.com/sitemap.xml", "type": "sitemap", "path_contains": "/news/", "max": 5},
    {"name": "Claude Code Releases", "url": "https://github.com/anthropics/claude-code/releases.atom", "type": "rss", "max": 5},
    {"name": "Google AI", "url": "https://blog.google/technology/ai/rss/", "type": "rss", "max": 5},
    {"name": "Gemini CLI Releases", "url": "https://github.com/google-gemini/gemini-cli/releases.atom", "type": "rss", "max": 5},
    {"name": "Hermes Agent Releases", "url": "https://github.com/NousResearch/hermes-agent/releases.atom", "type": "rss", "max": 5},
    {"name": "OpenClaw Releases", "url": "https://github.com/openclaw/openclaw/releases.atom", "type": "rss", "max": 5},
    {"name": "Transformers Releases", "url": "https://github.com/huggingface/transformers/releases.atom", "type": "rss", "max": 4},
]

DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d",
]


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _parse_date(date_str):
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    return None


def _clean(text, limit=None):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", html.unescape(text)).strip()
    if limit:
        text = text[:limit]
    return text


def _should_drop(title, drop_patterns):
    low = title.lower()
    return any(p in low for p in drop_patterns)


def _title_fingerprint(title):
    """Alpha and digit-runs kept separate so "qwen3.6-27b" and "qwen 3.6 27b is out" overlap. For fuzzy dedup."""
    stop = {"the", "and", "for", "with", "from", "out", "new", "now", "are", "was",
            "has", "is", "it", "be", "to", "in", "on", "of", "an", "as", "or", "but",
            "this", "that", "you", "your"}
    toks = re.findall(r"[a-z]+|\d+(?:\.\d+)?[a-z]?", title.lower())
    return {t for t in toks if t not in stop and len(t) >= 2}


def _is_duplicate(new_title, kept_titles, threshold=0.55):
    """True if new_title shares >= threshold of its significant tokens with any kept title."""
    new_fp = _title_fingerprint(new_title)
    if not new_fp:
        return False
    for kt in kept_titles:
        kt_fp = _title_fingerprint(kt)
        if not kt_fp:
            continue
        overlap = len(new_fp & kt_fp) / max(len(new_fp), 1)
        if overlap >= threshold:
            return True
    return False


def _domain(url):
    m = re.match(r"https?://(?:www\.)?([^/]+)/", url or "")
    return m.group(1).lower() if m else ""


def fetch_rss(src):
    name, url, max_n = src["name"], src["url"], src["max"]
    drop_patterns = src.get("drop_patterns", [])
    try:
        data = _get(url)
    except Exception as e:
        return [{"source": name, "title": f"Fetch error: {e}", "link": "", "pub": "", "meta": ""}]
    try:
        root = ET.fromstring(data)
    except Exception:
        return [{"source": name, "title": "XML parse error", "link": "", "pub": "", "meta": ""}]

    cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    is_atom = root.tag.endswith("}feed") or root.tag == "feed"
    articles = []

    if is_atom:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry") or root.findall(".//entry")
        for entry in entries:
            title = "No title"
            for tag in (".//{http://www.w3.org/2005/Atom}title", ".//title"):
                el = entry.find(tag)
                if el is not None and el.text:
                    title = html.unescape(el.text.strip())
                    break
            link = ""
            for lnk in entry.findall(".//{http://www.w3.org/2005/Atom}link"):
                href = lnk.get("href")
                if not href:
                    continue
                if lnk.get("rel") == "alternate":
                    link = href
                    break
                if not link:
                    link = href
            pub_el = entry.find(".//{http://www.w3.org/2005/Atom}published")
            if pub_el is None:
                pub_el = entry.find(".//{http://www.w3.org/2005/Atom}updated")
            if pub_el is None:
                pub_el = entry.find(".//published") or entry.find(".//updated")
            pub_dt = _parse_date(pub_el.text if pub_el is not None else "")
            if pub_dt and pub_dt < cutoff:
                continue
            if drop_patterns and _should_drop(title, drop_patterns):
                continue
            if src.get("domain_blacklist") and _domain(link) in src["domain_blacklist"]:
                continue
            if _is_duplicate(title, [a["title"] for a in articles]):
                continue
            articles.append({
                "source": name, "title": _clean(title), "link": link,
                "pub": pub_dt.strftime("%m-%d %H:%M") if pub_dt else "", "meta": "",
            })
            if len(articles) >= max_n:
                break
    else:
        items = root.findall(".//item") or root.findall(".//entry")
        for item in items:
            title_el = item.find("title")
            link_el = item.find("link")
            title = _clean(title_el.text if title_el is not None else "")
            link = (link_el.text or "").strip() if link_el is not None else ""
            pub_el = item.find("pubDate") or item.find("published") or item.find("updated")
            pub_dt = _parse_date(pub_el.text if pub_el is not None else "")
            if pub_dt and pub_dt < cutoff:
                continue
            if drop_patterns and _should_drop(title, drop_patterns):
                continue
            if src.get("domain_blacklist") and _domain(link) in src["domain_blacklist"]:
                continue
            if _is_duplicate(title, [a["title"] for a in articles]):
                continue
            articles.append({
                "source": name, "title": title, "link": link,
                "pub": pub_dt.strftime("%m-%d %H:%M") if pub_dt else "", "meta": "",
            })
            if len(articles) >= max_n:
                break
    return articles


def fetch_reddit_json(src):
    name, url, max_n = src["name"], src["url"], src["max"]
    drop_patterns = src.get("drop_patterns", [])
    try:
        data = json.loads(_get(url))
    except Exception as e:
        return [{"source": name, "title": f"Fetch error: {e}", "link": "", "pub": "", "meta": ""}]

    cutoff_epoch = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).timestamp()
    candidates = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied") or d.get("pinned"):
            continue
        score = d.get("score", 0)
        if score < REDDIT_MIN_SCORE:
            continue
        if d.get("created_utc", 0) < cutoff_epoch:
            continue
        title = html.unescape(d.get("title", "")).strip()
        if drop_patterns and _should_drop(title, drop_patterns):
            continue
        candidates.append({
            "source": name,
            "title": title,
            "link": f"https://reddit.com{d.get('permalink', '')}",
            "pub": datetime.utcfromtimestamp(d["created_utc"]).strftime("%m-%d %H:%M") if d.get("created_utc") else "",
            "meta": f"{score}↑ · {d.get('num_comments', 0)} cmts",
            "_score": score,
        })
    candidates.sort(key=lambda a: a["_score"], reverse=True)
    deduped = []
    for c in candidates:
        if _is_duplicate(c["title"], [d["title"] for d in deduped]):
            continue
        deduped.append(c)
        if len(deduped) >= max_n:
            break
    for a in deduped:
        a.pop("_score", None)
    return deduped


def fetch_sitemap(src):
    """Read recently modified announcement URLs from an official sitemap."""
    name, url, max_n = src["name"], src["url"], src["max"]
    try:
        root = ET.fromstring(_get(url))
    except Exception as e:
        return [{"source": name, "title": f"Fetch error: {e}", "link": "", "pub": "", "meta": ""}]

    cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    candidates = []
    for node in root.iter():
        if not node.tag.endswith("url"):
            continue
        loc = ""
        lastmod = ""
        for child in node:
            if child.tag.endswith("loc"):
                loc = (child.text or "").strip()
            elif child.tag.endswith("lastmod"):
                lastmod = (child.text or "").strip()
        if not loc or src.get("path_contains", "") not in loc:
            continue
        pub_dt = _parse_date(lastmod)
        if pub_dt and pub_dt < cutoff:
            continue
        candidates.append((pub_dt or datetime.min, loc))

    candidates.sort(reverse=True)
    articles = []
    for pub_dt, link in candidates[:max_n]:
        slug = link.rstrip("/").rsplit("/", 1)[-1]
        title = slug.replace("-", " ").strip().title()
        try:
            page = _get(link)
            match = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
            if match:
                title = _clean(match.group(1)).split(" | Anthropic")[0].strip()
        except Exception:
            pass
        articles.append({
            "source": name, "title": title, "link": link,
            "pub": pub_dt.strftime("%m-%d %H:%M") if pub_dt != datetime.min else "", "meta": "",
        })
    return articles


def fetch(src):
    t = src["type"]
    if t == "rss":
        return fetch_rss(src)
    if t == "reddit_json":
        return fetch_reddit_json(src)
    if t == "sitemap":
        return fetch_sitemap(src)
    return []


def main():
    all_articles = []
    per_source = {}
    for src in SOURCES:
        items = fetch(src)
        per_source[src["name"]] = items
        all_articles.extend(items)

    print(f"AI BRIEF — {datetime.utcnow().strftime('%b %d, %Y')} UTC\n")
    if not all_articles:
        print("No articles in lookback window.")
        sys.exit(0)

    for src in SOURCES:
        items = per_source[src["name"]]
        if not items:
            continue
        print(f"\n📰 {src['name']}")
        for a in items:
            meta = f" ({a['meta']})" if a.get("meta") else ""
            print(f"  • {a['title']}{meta}")
            if a["link"]:
                print(f"    {a['link']}")


if __name__ == "__main__":
    main()
