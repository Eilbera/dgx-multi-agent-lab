#!/usr/bin/env python3
"""Emit a deterministic shard of full transcripts awaiting semantic review."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from youtube_transcript_ingest import DB_PATH, ROOT, load_selection_ids, video_shard

REVIEW_DIR = ROOT / "state/transcript_reviews"
SELECTION_FILE = ROOT / "state/daily_selection/current.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, default=10)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--hours", type=float, default=48.0)
    args = parser.parse_args()
    if not 0 <= args.shard < args.shards:
        raise SystemExit("shard must be between 0 and shards-1")

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT v.video_id,v.title,v.url,v.published,v.channel,v.channel_id,v.tier,v.kind,
               v.source_score,v.source_rank,t.transcript_path,t.segment_count,t.word_count,
               t.duration,t.language
        FROM videos v
        JOIN transcript_status t ON t.video_id=v.video_id
        WHERE t.status='available' AND v.published >= datetime('now', ?)
        ORDER BY v.tier ASC,v.source_score DESC,v.published ASC
    """, (f"-{args.hours} hours",)).fetchall()
    db.close()

    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    selected_ids = set(load_selection_ids(SELECTION_FILE)) if SELECTION_FILE.exists() else set()
    queue = []
    for raw in rows:
        row = dict(raw)
        if row["video_id"] not in selected_ids:
            continue
        if video_shard(row["video_id"], args.shards) != args.shard:
            continue
        output = REVIEW_DIR / f"{row['video_id']}.json"
        if output.exists():
            continue
        row["review_output_path"] = str(output)
        row["required_read"] = (
            f"Read every line of {row['transcript_path']} from first to last. The file contains "
            f"{row['segment_count']} timestamped segments and {row['word_count']} words."
        )
        queue.append(row)
        if len(queue) >= args.limit:
            break

    print(json.dumps({
        "shard": args.shard,
        "shards": args.shards,
        "queued": len(queue),
        "videos": queue,
        "review_schema": {
            "video_id": "string",
            "transcript_fully_read": True,
            "segment_count": "exact integer from queue",
            "word_count": "exact integer from queue",
            "first_timestamp": "from first transcript line",
            "last_timestamp": "from final transcript line",
            "summary": "For transcripts under 200 words: 25-80 words. Otherwise: 90-140 words. Cover thesis, evidence, conclusion, and limitations without padding.",
            "key_claims": "array of consequential claims",
            "limitations": "array of caveats, bias, sponsorship, and missing evidence",
            "novelty": "New | Material update | Recycled",
            "importance_score": "integer 0-100",
            "story_key": "short canonical topic identifier for deduplication",
            "primary_source_queries": "array of claims/artifacts the morning synthesizer must verify",
            "best_timestamps": "3-5 timestamp strings",
            "reviewed_at": "UTC ISO timestamp"
        }
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
