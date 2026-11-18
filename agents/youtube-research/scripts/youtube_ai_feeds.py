#!/usr/bin/env python3
"""Persist and emit recent uploads from verified AI YouTube channels.

Discovery uses YouTube's public per-channel Atom feeds. Entries are persisted in
SQLite so high-volume channels do not lose videos when the feed's recent window
rolls over. No API key or third-party Python package is required.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import random
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


def load_channels(path: Path) -> list[dict]:
    """Load a ranked external watchlist and normalize it for collection."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("channels") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("watchlist must contain a channels array")
    channels = []
    seen = set()
    for row in rows:
        channel_id = row.get("channel_id") or row.get("id")
        if not channel_id or channel_id in seen:
            raise ValueError(f"missing or duplicate channel ID: {channel_id}")
        seen.add(channel_id)
        channel = {
            "name": row["name"],
            "handle": row.get("handle", ""),
            "id": channel_id,
            "channel_url": row.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}",
            "tier": int(row["tier"]),
            "kind": row["kind"],
        }
        if "score" in row:
            channel["score"] = row["score"]
        if "rank" in row:
            channel["rank"] = row["rank"]
        channels.append(channel)
    return channels


WATCHLIST_PATH = Path.home() / ".hermes/profiles/youtube-ai-researcher/research/youtube_top500_verified.json"

CHANNELS = [
    # Official labs, vendors, research organizations, and developer channels.
    {"name":"OpenAI","handle":"OpenAI","id":"UCXZCJLdBC09xxGZ6gcdrc6A","tier":1,"kind":"primary"},
    {"name":"Anthropic","handle":"anthropic-ai","id":"UCrDwWp7EBBv4NwvScIpBDOA","tier":1,"kind":"primary"},
    {"name":"Google DeepMind","handle":"googledeepmind","id":"UCP7jMXSY2xbc3KCAE0MHQ-A","tier":1,"kind":"primary"},
    {"name":"Google for Developers","handle":"GoogleDevelopers","id":"UC_x5XG1OV2P6uZZ5FSM9Ttw","tier":1,"kind":"vendor-technical"},
    {"name":"AI at Meta","handle":"aiatmeta","id":"UC5qxlwEKM7-5YZudb24l0bg","tier":1,"kind":"primary"},
    {"name":"NVIDIA","handle":"NVIDIA","id":"UCHuiy8bXnmK5nisYHUd1J5g","tier":1,"kind":"vendor"},
    {"name":"NVIDIA Developer","handle":"NVIDIADeveloper","id":"UCBHcMCGaiJhv-ESTcWGJPcw","tier":1,"kind":"vendor-technical"},
    {"name":"Hugging Face","handle":"HuggingFace","id":"UCHlNU7kIZhRgSbhHvFoy72w","tier":1,"kind":"primary"},
    {"name":"Mistral","handle":"MistralAI","id":"UC5-pBdfdA3KUo-vq72l-umA","tier":1,"kind":"primary"},
    {"name":"Cohere","handle":"CohereAI","id":"UCAKTUy0tz47ZY02DFpxMqoQ","tier":1,"kind":"primary"},
    {"name":"DeepLearningAI","handle":"DeepLearningAI","id":"UCcIXc5mJsHVYTZR1maL5l9w","tier":1,"kind":"education-news"},
    {"name":"Google Cloud Tech","handle":"GoogleCloudTech","id":"UCJS9pqu9BzkAMNTmzNMNhvg","tier":2,"kind":"vendor-technical"},
    {"name":"Google Research","handle":"GoogleResearch","id":"UCT-VzthVAM_4ohDdKa-BbXA","tier":2,"kind":"primary"},
    {"name":"Microsoft Research","handle":"MicrosoftResearch","id":"UCCb9_Kn8F_Opb3UCGm-lILQ","tier":2,"kind":"primary"},
    {"name":"Stanford HAI","handle":"StanfordHAI","id":"UChugFTK0KyrES9terTid8vA","tier":2,"kind":"primary"},
    {"name":"MIT CSAIL","handle":"MITCSAIL","id":"UCBpxspUNl1Th33XbugiHJzw","tier":2,"kind":"primary"},
    {"name":"Ai2","handle":"ai2","id":"UCEqgmyWChwvt6MFGGlmUQCQ","tier":2,"kind":"primary"},
    {"name":"EleutherAI","handle":"EleutherAI","id":"UCljbpGFxy_7i4bMDXyuB96A","tier":2,"kind":"primary"},
    {"name":"Together AI","handle":"TogetherAI","id":"UCgiSqzt-AFeQFa6-pAxTIvA","tier":2,"kind":"vendor-technical"},
    {"name":"AWS Developers","handle":"AWSDevelopers","id":"UCT-nPlVzJI-ccQXlxjSvJmw","tier":2,"kind":"vendor-technical"},
    {"name":"ElevenLabs","handle":"ElevenLabs","id":"UC-ew9TfeD887qUSiWWAAj1w","tier":2,"kind":"vendor"},
    # Independent technical analysis and fast discovery feeds.
    {"name":"AI Explained","handle":"aiexplained-official","id":"UCNJ1Ymd5yFuUPtn21xtRbbw","tier":1,"kind":"analysis"},
    {"name":"Yannic Kilcher","handle":"YannicKilcher","id":"UCZHmQk67mSJgfCCTn7xBfew","tier":1,"kind":"analysis"},
    {"name":"Machine Learning Street Talk","handle":"MachineLearningStreetTalk","id":"UCMLtBahI5DMrt0NPvDSoIRQ","tier":1,"kind":"interview-analysis"},
    {"name":"Nathan Lambert","handle":"natolambert","id":"UCayQjCHzSnqPszcHNMmfkSg","tier":1,"kind":"expert"},
    {"name":"Sebastian Raschka","handle":"SebastianRaschka","id":"UC_CzsS7UTjcxJ-xXp1ftxtA","tier":1,"kind":"expert"},
    {"name":"Two Minute Papers","handle":"TwoMinutePapers","id":"UCbfYPyITQ-7l4upoX8nvctg","tier":1,"kind":"analysis"},
    {"name":"Latent Space","handle":"LatentSpacePod","id":"UCxBcwypKK-W3GHd_RZ9FZrQ","tier":1,"kind":"engineering-news"},
    {"name":"The TWIML AI Podcast","handle":"twimlai","id":"UC7kjWIK1H8tfmFlzZO-wHMw","tier":1,"kind":"interview-analysis"},
    {"name":"The Cognitive Revolution","handle":"CognitiveRevolutionPodcast","id":"UCjNRVMBVI30Sak_p6HRWhIA","tier":1,"kind":"interview"},
    {"name":"Matthew Berman","handle":"matthew_berman","id":"UCawZsQWqfGSbCI5yjkdVkTA","tier":2,"kind":"news-tools"},
    {"name":"Matt Wolfe","handle":"mreflow","id":"UChpleBmo18P08aKCIgti38g","tier":2,"kind":"news-tools"},
    {"name":"The AI Advantage","handle":"aiadvantage","id":"UCHhYXsLBEVVnbvsq57n1MTQ","tier":3,"kind":"discovery-only"},
    {"name":"Wes Roth","handle":"WesRoth","id":"UCqcbQf6yw5KzRoDDcZ_wBSw","tier":3,"kind":"discovery-only"},
    {"name":"MattVidPro AI","handle":"MattVidPro","id":"UC5Wz4fFacYuON6IKbhSa7Zw","tier":3,"kind":"discovery-only"},
    {"name":"The AI Grid","handle":"TheAIGRID","id":"UCbY9xX3_jW5c2fjlZVBI4cg","tier":3,"kind":"discovery-only"},
    {"name":"Fireship","handle":"Fireship","id":"UCsBjURrPoezykLs9EqgamOA","tier":2,"kind":"developer-news"},
    # Interviews, education, and conference archives.
    {"name":"Dwarkesh Patel","handle":"DwarkeshPatel","id":"UCXl4i9dYBrFOabk0xGmbkRA","tier":1,"kind":"interview"},
    {"name":"No Priors","handle":"NoPriorsPodcast","id":"UCSI7h9hydQ40K5MJHnCrQvw","tier":1,"kind":"interview"},
    {"name":"Lex Fridman","handle":"LexFridman","id":"UCSHZKyawb77ixDdsGog4iWA","tier":2,"kind":"interview"},
    {"name":"Stanford Online","handle":"stanfordonline","id":"UCBa5G_ESCn8Yd4vw5U-gIcg","tier":2,"kind":"education"},
    {"name":"Andrej Karpathy","handle":"AndrejKarpathy","id":"UCXUPKJO5MZQN11PqgIvyuvQ","tier":2,"kind":"expert"},
    {"name":"The Full Stack","handle":"FullStackDeepLearning","id":"UCVchfoB65aVtQiDITbGq2LQ","tier":2,"kind":"education"},
    {"name":"SlidesLive","handle":"SlidesLive","id":"UCkahKPXfDbGOd_JKqW_P5gw","tier":2,"kind":"conference"},
    {"name":"Conference on Language Modeling","handle":"colm_conf","id":"UCr4S7b9E82ZGVixVrvC5YjQ","tier":1,"kind":"conference"},
    {"name":"ICML","handle":"ICMLconf","id":"UC_JtgG9jWCnd9fU5upXboGQ","tier":2,"kind":"conference"},
    {"name":"ICLR","handle":"ICLRconf","id":"UCqxFGrNL5nX10lS62bswp9w","tier":2,"kind":"conference"},
]

SEED_CHANNELS = CHANNELS
if WATCHLIST_PATH.exists():
    CHANNELS = load_channels(WATCHLIST_PATH)

NS = {"atom":"http://www.w3.org/2005/Atom", "yt":"http://www.youtube.com/xml/schemas/2015"}
UA = "Mozilla/5.0 (compatible; Hermes-YouTube-AI-Researcher/2.0)"
DEFAULT_DB = Path.home() / ".hermes/profiles/youtube-ai-researcher/state/youtube_ai_monitor.sqlite3"


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def fetch(ch: dict, timeout: int = 25, attempts: int = 3) -> tuple[list[dict], dict | None]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['id']}"
    last_exc = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                root = ET.fromstring(r.read())
            feed_channel_id = (root.findtext("yt:channelId", default="", namespaces=NS) or "").strip()
            # YouTube's feed-level yt:channelId currently omits the leading "UC"
            # even though the canonical channel/feed URL retains it.
            valid_feed_ids = {ch["id"], ch["id"][2:] if ch["id"].startswith("UC") else ch["id"]}
            if feed_channel_id and feed_channel_id not in valid_feed_ids:
                raise ValueError(f"feed channel ID {feed_channel_id} != configured {ch['id']}")
            rows = []
            for e in root.findall("atom:entry", NS):
                video_id = (e.findtext("yt:videoId", default="", namespaces=NS) or "").strip()
                published = (e.findtext("atom:published", default="", namespaces=NS) or "").strip()
                updated = (e.findtext("atom:updated", default="", namespaces=NS) or "").strip()
                title = (e.findtext("atom:title", default="", namespaces=NS) or "").strip()
                if not (video_id and published and title):
                    continue
                rows.append({
                    "video_id": video_id, "title": title,
                    "published": parse_time(published).isoformat().replace("+00:00", "Z"),
                    "feed_updated": parse_time(updated).isoformat().replace("+00:00", "Z") if updated else None,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "channel": ch["name"], "channel_url": ch.get("channel_url") or f"https://www.youtube.com/@{ch['handle']}",
                    "channel_id": ch["id"], "tier": ch["tier"], "kind": ch["kind"],
                    "source_score": ch.get("score"), "source_rank": ch.get("rank"),
                })
            return rows, None
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep((0.6 * (2 ** attempt)) + random.random() * 0.4)
    return [], {"channel": ch["name"], "feed": url, "error": f"{type(last_exc).__name__}: {last_exc}"}


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, channel TEXT NOT NULL,
            title TEXT NOT NULL, url TEXT NOT NULL, published TEXT NOT NULL,
            feed_updated TEXT, discovered TEXT NOT NULL, tier INTEGER NOT NULL, kind TEXT NOT NULL,
            channel_url TEXT NOT NULL, source_score INTEGER, source_rank INTEGER
        );
        CREATE TABLE IF NOT EXISTS channel_status (
            channel_id TEXT PRIMARY KEY, channel TEXT NOT NULL, last_success TEXT,
            last_error TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published);
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
    if "source_score" not in columns:
        db.execute("ALTER TABLE videos ADD COLUMN source_score INTEGER")
    if "source_rank" not in columns:
        db.execute("ALTER TABLE videos ADD COLUMN source_rank INTEGER")
    return db


def persist(db: sqlite3.Connection, now_s: str, results: list[tuple[dict, list[dict], dict | None]]) -> None:
    for ch, rows, err in results:
        if err:
            db.execute("""INSERT INTO channel_status(channel_id,channel,last_error,consecutive_failures)
                        VALUES(?,?,?,1) ON CONFLICT(channel_id) DO UPDATE SET
                        channel=excluded.channel,last_error=excluded.last_error,
                        consecutive_failures=channel_status.consecutive_failures+1""",
                       (ch["id"], ch["name"], err["error"]))
            continue
        db.execute("""INSERT INTO channel_status(channel_id,channel,last_success,last_error,consecutive_failures)
                    VALUES(?,?,?,NULL,0) ON CONFLICT(channel_id) DO UPDATE SET
                    channel=excluded.channel,last_success=excluded.last_success,last_error=NULL,consecutive_failures=0""",
                   (ch["id"], ch["name"], now_s))
        for v in rows:
            db.execute("""INSERT INTO videos(video_id,channel_id,channel,title,url,published,feed_updated,
                        discovered,tier,kind,channel_url,source_score,source_rank) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(video_id) DO UPDATE SET title=excluded.title,url=excluded.url,
                        feed_updated=excluded.feed_updated,channel=excluded.channel,tier=excluded.tier,
                        kind=excluded.kind,channel_url=excluded.channel_url,
                        source_score=excluded.source_score,source_rank=excluded.source_rank""",
                       (v["video_id"],v["channel_id"],v["channel"],v["title"],v["url"],v["published"],
                        v["feed_updated"],now_s,v["tier"],v["kind"],v["channel_url"],
                        v.get("source_score"),v.get("source_rank")))
    # Keep a year of metadata; enough for gap recovery without unbounded growth.
    prune = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365)).isoformat().replace("+00:00", "Z")
    db.execute("DELETE FROM videos WHERE published < ?", (prune,))
    db.commit()


def build_payload(
    *,
    generated_at: str,
    window_hours: float,
    channels_checked: int,
    videos: list[dict],
    errors: list[dict],
    unhealthy: list[dict],
) -> dict:
    """Build collector output with explicit, mechanically reconcilable coverage."""
    discovered = len({video["video_id"] for video in videos})
    return {
        "generated_at": generated_at,
        "window_hours": window_hours,
        "channels_configured": channels_checked,
        "channels_checked": channels_checked,
        "videos_found": len(videos),
        "coverage": {
            "configured_channels": channels_checked,
            "live_feed_successes": channels_checked - len(errors),
            "feed_failures": len(errors),
            "persisted_candidates": len(videos),
            "manual_web_additions": 0,
            "deduplicated_discovered": discovered,
        },
        "videos": videos,
        "current_run_errors": errors,
        "unhealthy_channels": unhealthy,
        "notes": [
            "YouTube channel feeds commonly expose only about 15 recent entries; the SQLite store is refreshed every four hours.",
            "xAI, BAIR, and NeurIPS have no verified dedicated official YouTube channel; use official-site and targeted web-search fallbacks.",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=36.0)
    ap.add_argument("--tier", type=int, choices=[1,2,3], help="emit only this tier")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--collect-only", action="store_true")
    args = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    now_s = now.isoformat().replace("+00:00", "Z")
    selected = [c for c in CHANNELS if args.tier is None or c["tier"] == args.tier]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        fetched = list(ex.map(fetch, selected))
    for ch, (rows, err) in zip(selected, fetched):
        results.append((ch, rows, err))
    db = open_db(args.db)
    persist(db, now_s, results)
    if args.collect_only:
        db.close()
        return 0
    cutoff = (now - dt.timedelta(hours=args.hours)).isoformat().replace("+00:00", "Z")
    selected_ids = [channel["id"] for channel in selected]
    placeholders = ",".join("?" for _ in selected_ids)
    rows = db.execute(f"""SELECT video_id,title,published,url,channel,channel_url,channel_id,tier,kind,discovered,
                                source_score,source_rank
                         FROM videos WHERE published >= ? AND channel_id IN ({placeholders})
                         ORDER BY tier ASC, source_score DESC, published DESC""", (cutoff, *selected_ids)).fetchall()
    videos = [dict(zip(("video_id","title","published","url","channel","channel_url","channel_id","tier","kind","discovered","source_score","source_rank"), r)) for r in rows]
    errors = [err for _,_,err in results if err]
    unhealthy = [dict(zip(("channel","consecutive_failures","last_error"), r)) for r in db.execute(
        "SELECT channel,consecutive_failures,last_error FROM channel_status WHERE consecutive_failures > 0 ORDER BY consecutive_failures DESC")]
    db.close()
    payload = build_payload(
        generated_at=now_s,
        window_hours=args.hours,
        channels_checked=len(selected),
        videos=videos,
        errors=errors,
        unhealthy=unhealthy,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
