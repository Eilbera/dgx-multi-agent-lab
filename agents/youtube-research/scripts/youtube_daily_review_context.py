#!/usr/bin/env python3
"""Build morning context from collector data and completed full-transcript reviews."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/USER/.hermes/profiles/youtube-ai-researcher")
SCRIPTS = ROOT / "scripts"
DB_PATH = ROOT / "state/youtube_ai_monitor.sqlite3"
REVIEW_DIR = ROOT / "state/transcript_reviews"
SELECTION_FILE = ROOT / "state/daily_selection/current.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=36.0)
    args = parser.parse_args()
    collector = subprocess.run(
        [sys.executable, str(SCRIPTS / "youtube_ai_feeds.py"), "--hours", str(args.hours)],
        text=True, capture_output=True, check=True, timeout=600,
    )
    payload = json.loads(collector.stdout)
    selection = json.loads(SELECTION_FILE.read_text(encoding="utf-8")) if SELECTION_FILE.exists() else {"videos": []}
    selected_videos = selection.get("videos", [])
    video_ids = {video["video_id"] for video in selected_videos}

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    statuses = {}
    if video_ids:
        placeholders = ",".join("?" for _ in video_ids)
        rows = db.execute(
            f"SELECT video_id,status,transcript_path,segment_count,word_count,duration,language,error "
            f"FROM transcript_status WHERE video_id IN ({placeholders})",
            tuple(video_ids),
        ).fetchall()
        statuses = {row["video_id"]: dict(row) for row in rows}
    db.close()

    reviews = []
    invalid_reviews = []
    for video in selected_videos:
        path = REVIEW_DIR / f"{video['video_id']}.json"
        if not path.exists():
            continue
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
            status = statuses.get(video["video_id"], {})
            summary_words = len(str(review.get("summary", "")).split())
            expected_summary = 25 <= summary_words <= 80 if int(status.get("word_count") or 0) < 200 else 90 <= summary_words <= 140
            valid = (
                review.get("video_id") == video["video_id"]
                and review.get("transcript_fully_read") is True
                and review.get("segment_count") == status.get("segment_count")
                and review.get("word_count") == status.get("word_count")
                and expected_summary
            )
            if not valid:
                invalid_reviews.append(video["video_id"])
                continue
            reviews.append({"video": video, "transcript": status, "review": review})
        except Exception:
            invalid_reviews.append(video["video_id"])

    available = sum(status.get("status") == "available" for status in statuses.values())
    unavailable = sum(status.get("status") == "unavailable" for status in statuses.values())
    errors = sum(status.get("status") == "error" for status in statuses.values())
    scheduled = sum(status.get("status") == "scheduled" for status in statuses.values())
    pending_fetch = len(video_ids) - len(statuses)
    coverage = {
        "videos_discovered": len(payload["videos"]),
        "videos_selected": len(video_ids),
        "scheduled_events_not_started": scheduled,
        "videos_in_review_scope": len(video_ids) - scheduled,
        "transcripts_available": available,
        "transcripts_unavailable": unavailable,
        "transcript_fetch_errors": errors,
        "transcript_fetch_pending": pending_fetch,
        "full_transcript_reviews_complete": len(reviews),
        "full_transcript_reviews_pending": max(0, available - len(reviews)),
        "invalid_review_files": invalid_reviews,
        "coverage_complete": pending_fetch == 0 and errors == 0 and len(reviews) == available,
    }
    print(json.dumps({
        "collector": payload,
        "selection": selection,
        "transcript_review_coverage": coverage,
        "completed_reviews": sorted(
            reviews,
            key=lambda item: (
                -int(item["review"].get("importance_score", 0)),
                int(item["video"].get("source_rank") or 9999),
            ),
        ),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
