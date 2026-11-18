#!/usr/bin/env python3
"""Merge, verify, score, and rank candidate AI YouTube channels."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path("/home/USER/.hermes/profiles/youtube-ai-researcher")
RESEARCH = ROOT / "research"
OUTPUT = RESEARCH / "youtube_top500_verified.json"
UA = "Mozilla/5.0 (compatible; Hermes-YouTube-AI-Researcher/3.0)"
NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
AI_RE = re.compile(
    r"(?i)\b(ai|artificial intelligence|machine learning|deep learning|neural|llm|language model|transformer|"
    r"generative|diffusion|computer vision|robot|reinforcement learning|nlp|speech|multimodal|agent|inference|"
    r"training|alignment|interpretability|foundation model|data science|mlops|cuda|gpu|pytorch|tensorflow|jax)\b"
)
LOW_SIGNAL_RE = re.compile(r"(?i)(get rich|make money|passive income|faceless|top \d+ ai tools|you won.t believe|shocked|insane)")


def candidates_from_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("candidates", "channels", "results"):
            if isinstance(data.get(key), list):
                return [row for row in data[key] if isinstance(row, dict)]
    return []


def existing_candidates() -> list[dict]:
    import youtube_ai_feeds as feeds
    rows = []
    for channel in feeds.SEED_CHANNELS:
        rows.append({
            **channel,
            "channel_id": channel["id"],
            "channel_url": f"https://www.youtube.com/@{channel['handle']}",
            "category": channel.get("kind", "existing"),
            "language_or_region": "English/global",
            "official_status": "verified-existing",
            "feed_verified": True,
            "verification_notes": "Existing monitored source with canonical-ID tests.",
            "source_files": ["youtube_ai_feeds.py"],
        })
    return rows


def normalize(row: dict, source: str) -> dict | None:
    channel_id = row.get("channel_id") or row.get("id")
    if not isinstance(channel_id, str) or not channel_id.startswith("UC") or len(channel_id) < 20:
        return None
    channel_url = row.get("channel_url") or row.get("url") or f"https://www.youtube.com/channel/{channel_id}"
    handle = row.get("handle") or row.get("uploader_id") or ""
    if isinstance(handle, str):
        handle = handle.lstrip("@")
    if not handle and "/@" in channel_url:
        handle = channel_url.split("/@", 1)[1].split("/", 1)[0]
    return {
        "name": str(row.get("name") or row.get("channel") or "").strip(),
        "handle": handle,
        "channel_id": channel_id,
        "channel_url": channel_url,
        "category": str(row.get("category") or row.get("kind") or "unclassified"),
        "language_or_region": str(row.get("language_or_region") or "unknown"),
        "kind": str(row.get("kind") or row.get("category") or "unclassified"),
        "official_status": str(row.get("official_status") or "unverified"),
        "verification_notes": str(row.get("verification_notes") or ""),
        "discovery_url": str(row.get("discovery_url") or ""),
        "sample_titles": list(row.get("sample_titles") or []),
        "query_hits": list(row.get("query_hits") or []),
        "source_files": sorted(set(list(row.get("source_files") or []) + [source])),
    }


def merge_candidates() -> dict[str, dict]:
    merged: dict[str, dict] = {}
    sources = [("youtube_ai_feeds.py", existing_candidates())]
    for path in sorted(RESEARCH.glob("candidates_*.json")):
        sources.append((path.name, candidates_from_file(path)))
    for source, rows in sources:
        for raw in rows:
            row = normalize(raw, source)
            if not row:
                continue
            current = merged.get(row["channel_id"])
            if current is None:
                merged[row["channel_id"]] = row
                continue
            for key in ("sample_titles", "query_hits", "source_files"):
                current[key] = sorted(set(current.get(key, []) + row.get(key, [])))
            for key in ("name", "handle", "channel_url", "category", "language_or_region", "kind", "official_status", "verification_notes", "discovery_url"):
                if (not current.get(key) or current[key] in ("unknown", "unverified", "unclassified")) and row.get(key):
                    current[key] = row[key]
    return merged


def verify_feed(row: dict) -> dict:
    channel_id = row["channel_id"]
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    result = {**row, "feed_url": feed_url, "feed_verified": False, "feed_error": None, "feed_titles": [], "newest_published": None}
    try:
        req = urllib.request.Request(feed_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as response:
            root = ET.fromstring(response.read())
        feed_id = (root.findtext("yt:channelId", default="", namespaces=NS) or "").strip()
        valid = {channel_id, channel_id[2:]}
        if feed_id and feed_id not in valid:
            raise ValueError(f"feed ID {feed_id} does not match {channel_id}")
        feed_title = (root.findtext("atom:title", default="", namespaces=NS) or "").strip()
        entries = root.findall("atom:entry", NS)
        titles, dates = [], []
        for entry in entries:
            title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
            published = (entry.findtext("atom:published", default="", namespaces=NS) or "").strip()
            if title:
                titles.append(title)
            if published:
                dates.append(published)
        result.update({
            "feed_verified": True,
            "feed_channel_title": feed_title,
            "feed_titles": titles[:15],
            "newest_published": max(dates) if dates else None,
        })
        if not result["name"]:
            result["name"] = feed_title
    except Exception as exc:
        result["feed_error"] = f"{type(exc).__name__}: {exc}"
    return result


def infer_kind(row: dict) -> str:
    kind = row.get("kind", "unclassified").lower().replace("_", "-")
    canonical = {
        "primary", "academic", "conference", "expert", "vendor-technical",
        "engineering-news", "education", "education-news", "interview-analysis",
        "interview", "analysis", "developer-news", "vendor", "news-tools", "discovery-only",
    }
    if kind in canonical:
        return kind
    if any(token in kind for token in ("conference", "workshop", "symposium", "summer-school", "summit", "event")):
        return "conference"
    if any(token in kind for token in ("official lab", "model-lab", "research-lab", "nonprofit-research", "open-source-research")):
        return "primary"
    if any(token in kind for token in ("university", "institutional", "research-seminar", "professional-society")):
        return "academic"
    if any(token in kind for token in ("interview", "podcast", "researcher-builder")):
        return "interview-analysis"
    if any(token in kind for token in ("educator", "education", "training-vendor", "academic-media")):
        return "education"
    if any(token in kind for token in ("tool-news", "commentary")):
        return "news-tools"
    if any(token in kind for token in ("vendor", "developer", "open-source", "technical-media", "ai-engineering")):
        return "vendor-technical"
    if any(token in kind for token in ("researcher", "paper-analysis")):
        return "expert"
    text = " ".join([row.get("name", ""), row.get("category", "")]).lower()
    if any(word in text for word in ("conference", "workshop", "symposium", "neurips", "icml", "iclr", "cvpr", "acl ")):
        return "conference"
    if any(word in text for word in ("university", "institute", "laboratory", " lab", "research", "seminar")):
        return "academic"
    if any(word in text for word in ("podcast", "interview")):
        return "interview-analysis"
    if any(word in text for word in ("developer", "engineering", "mlops", "cloud", "data")):
        return "vendor-technical"
    return "analysis"


def score(row: dict) -> dict:
    kind = infer_kind(row)
    base = {
        "primary": 76, "academic": 72, "conference": 72, "expert": 70,
        "vendor-technical": 65, "engineering-news": 63, "education": 62,
        "education-news": 61, "interview-analysis": 60, "interview": 58,
        "analysis": 57, "developer-news": 54, "vendor": 52,
        "news-tools": 45, "discovery-only": 38,
    }.get(kind, 50)
    status = row.get("official_status", "").lower()
    trusted_status = status not in ("", "unknown", "unverified") and any(
        token in status for token in ("verified", "official", "primary")
    )
    if trusted_status:
        base += 7
    source_count = len(row.get("source_files", []))
    base += min(5, max(0, source_count - 1))
    base += min(6, len(row.get("query_hits", [])))
    titles = row.get("feed_titles", []) or row.get("sample_titles", [])
    if titles:
        relevant = sum(bool(AI_RE.search(title)) for title in titles)
        ratio = relevant / len(titles)
        base += round(ratio * 10)
        if ratio < 0.2:
            base -= 10
        if any(LOW_SIGNAL_RE.search(title) for title in titles):
            base -= 8
    newest = row.get("newest_published")
    if newest:
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))).days
            if age <= 90:
                base += 7
            elif age <= 365:
                base += 4
            elif age <= 1095:
                base += 0
            else:
                base -= 8
        except ValueError:
            pass
    if kind == "discovery-only":
        base = min(base, 55)
    if row.get("source_files") == ["candidates_search.json"] and not trusted_status:
        base = min(base, 59)
    final = max(0, min(100, base))
    tier = 1 if final >= 78 else 2 if final >= 60 else 3
    return {**row, "kind": kind, "score": final, "tier": tier}


def main() -> int:
    merged = merge_candidates()
    rows = list(merged.values())
    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        for row in pool.map(verify_feed, rows):
            if row["feed_verified"]:
                verified.append(score(row))
    ranked = sorted(verified, key=lambda row: (-row["score"], row["name"].lower(), row["channel_id"]))
    selected = ranked[:500]
    for index, row in enumerate(selected, 1):
        row["rank"] = index
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate_files": sorted(path.name for path in RESEARCH.glob("candidates_*.json")),
        "merged_candidates": len(merged),
        "verified_feeds": len(verified),
        "selected_count": len(selected),
        "channels": selected,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"path": str(OUTPUT), "merged": len(merged), "verified": len(verified), "selected": len(selected)}))
    return 0 if len(selected) >= 500 else 2


if __name__ == "__main__":
    raise SystemExit(main())
