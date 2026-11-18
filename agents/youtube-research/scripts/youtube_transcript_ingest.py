#!/home/USER/.hermes/profiles/youtube-ai-researcher/.venv-transcripts/bin/python
"""Continuously fetch and persist complete transcripts for recent monitored videos."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

ROOT = Path("/home/USER/.hermes/profiles/youtube-ai-researcher")
DB_PATH = ROOT / "state/youtube_ai_monitor.sqlite3"
TRANSCRIPT_DIR = ROOT / "state/transcripts"
WORD_RE = re.compile(r"\b[\w’'-]+\b")


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def video_shard(video_id: str, shard_count: int) -> int:
    return int(hashlib.sha256(video_id.encode("utf-8")).hexdigest(), 16) % shard_count


def load_selection_ids(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    videos = payload.get("videos", [])
    ids = [row["video_id"] for row in videos if isinstance(row, dict) and row.get("video_id")]
    return list(dict.fromkeys(ids))


def build_transcript_record(video_id: str, segments: list[dict], language: str) -> dict:
    if not segments:
        raise ValueError("transcript contains no segments")
    normalized = [
        {"text": str(segment["text"]), "start": float(segment["start"]), "duration": float(segment["duration"])}
        for segment in segments
    ]
    full_text = " ".join(segment["text"] for segment in normalized)
    timestamped = "\n".join(f"{format_timestamp(segment['start'])} {segment['text']}" for segment in normalized)
    return {
        "video_id": video_id,
        "language": language,
        "segment_count": len(normalized),
        "word_count": len(WORD_RE.findall(full_text)),
        "first_timestamp": format_timestamp(normalized[0]["start"]),
        "last_segment_timestamp": format_timestamp(normalized[-1]["start"]),
        "duration": format_timestamp(normalized[-1]["start"] + normalized[-1]["duration"]),
        "full_text": full_text,
        "timestamped_text": timestamped,
        "segments": normalized,
    }


def ensure_status_table(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS transcript_status (
            video_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            transcript_path TEXT,
            segment_count INTEGER,
            word_count INTEGER,
            duration TEXT,
            language TEXT,
            attempted_at TEXT NOT NULL,
            error TEXT
        )
    """)
    db.commit()


def repair_text_exports(db: sqlite3.Connection) -> None:
    """Convert older JSON transcript paths to compact timestamped-text files."""
    rows = db.execute(
        "SELECT video_id,transcript_path FROM transcript_status WHERE status='available' AND transcript_path LIKE '%.json'"
    ).fetchall()
    for video_id, transcript_path in rows:
        manifest_path = Path(transcript_path)
        if not manifest_path.exists():
            continue
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
        text_path = manifest_path.with_suffix(".txt")
        text_path.write_text(record["timestamped_text"] + "\n", encoding="utf-8")
        db.execute("UPDATE transcript_status SET transcript_path=? WHERE video_id=?", (str(text_path), video_id))
    db.commit()


def fetch_one(row: dict) -> dict:
    video_id = row["video_id"]
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import GenericProxyConfig
        proxy_url = os.environ.get("YOUTUBE_TRANSCRIPT_PROXY_URL")
        proxy_config = GenericProxyConfig(http_url=proxy_url, https_url=proxy_url) if proxy_url else None
        transcript = YouTubeTranscriptApi(proxy_config=proxy_config).fetch(video_id)
        segments = [
            {"text": item.text, "start": item.start, "duration": item.duration}
            for item in transcript
        ]
        record = build_transcript_record(video_id, segments, getattr(transcript, "language_code", "unknown"))
        record["video"] = row
        manifest_path = TRANSCRIPT_DIR / f"{video_id}.json"
        text_path = TRANSCRIPT_DIR / f"{video_id}.txt"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        text_path.write_text(record["timestamped_text"] + "\n", encoding="utf-8")
        return {
            "video_id": video_id,
            "status": "available",
            "path": str(text_path),
            "segment_count": record["segment_count"],
            "word_count": record["word_count"],
            "duration": record["duration"],
            "language": record["language"],
            "error": None,
        }
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        lowered = message.lower()
        scheduled = any(token in lowered for token in ("premieres in", "premiered", "live event will begin"))
        permanent = any(token in lowered for token in ("disabled", "no transcript", "not available", "unavailable", "private"))
        return {
            "video_id": video_id,
            "status": "scheduled" if scheduled else "unavailable" if permanent else "error",
            "path": None,
            "segment_count": None,
            "word_count": None,
            "duration": None,
            "language": None,
            "error": message[:2000],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=48.0)
    parser.add_argument("--limit", type=int, default=0, help="maximum pending videos; 0 means all")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--retry-errors-after-hours", type=float, default=12.0)
    parser.add_argument("--selection-file", type=Path)
    args = parser.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    now_s = now.isoformat().replace("+00:00", "Z")
    cutoff = (now - dt.timedelta(hours=args.hours)).isoformat().replace("+00:00", "Z")
    retry_cutoff = (now - dt.timedelta(hours=args.retry_errors_after_hours)).isoformat().replace("+00:00", "Z")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    ensure_status_table(db)
    repair_text_exports(db)
    selection_ids = load_selection_ids(args.selection_file) if args.selection_file else []
    selection_clause = ""
    parameters: list[object] = [cutoff, retry_cutoff]
    if args.selection_file:
        if not selection_ids:
            rows = []
        else:
            placeholders = ",".join("?" for _ in selection_ids)
            selection_clause = f" AND v.video_id IN ({placeholders})"
            parameters.extend(selection_ids)
    if not args.selection_file or selection_ids:
        rows = db.execute(f"""
        SELECT v.video_id,v.title,v.url,v.published,v.channel,v.channel_id,v.tier,v.kind,
               v.source_score,v.source_rank
        FROM videos v
        LEFT JOIN transcript_status t ON t.video_id=v.video_id
        WHERE v.published >= ? AND v.source_rank IS NOT NULL
          AND (t.video_id IS NULL OR (t.status IN ('error','scheduled') AND t.attempted_at < ?))
          {selection_clause}
        ORDER BY CASE WHEN t.video_id IS NULL THEN 0 WHEN t.status='error' THEN 1 ELSE 2 END,
                 v.tier ASC,v.source_score DESC,v.published DESC
    """, tuple(parameters)).fetchall()
    candidates = [dict(row) for row in rows]
    if args.limit > 0:
        candidates = candidates[:args.limit]

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(fetch_one, candidates):
            results.append(result)
            db.execute("""
                INSERT INTO transcript_status(video_id,status,transcript_path,segment_count,word_count,duration,language,attempted_at,error)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(video_id) DO UPDATE SET status=excluded.status,transcript_path=excluded.transcript_path,
                    segment_count=excluded.segment_count,word_count=excluded.word_count,duration=excluded.duration,
                    language=excluded.language,attempted_at=excluded.attempted_at,error=excluded.error
            """, (
                result["video_id"], result["status"], result["path"], result["segment_count"],
                result["word_count"], result["duration"], result["language"], now_s, result["error"],
            ))
            db.commit()
    summary = {
        "generated_at": now_s,
        "window_hours": args.hours,
        "pending_found": len(rows),
        "attempted": len(results),
        "available": sum(result["status"] == "available" for result in results),
        "unavailable": sum(result["status"] == "unavailable" for result in results),
        "scheduled": sum(result["status"] == "scheduled" for result in results),
        "errors": sum(result["status"] == "error" for result in results),
        "segments_saved": sum(result["segment_count"] or 0 for result in results),
        "words_saved": sum(result["word_count"] or 0 for result in results),
    }
    db.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
