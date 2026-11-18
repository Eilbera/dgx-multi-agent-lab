#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from youtube_transcript_ingest import build_transcript_record, load_selection_ids, video_shard


class TranscriptPipelineTests(unittest.TestCase):
    def test_build_record_preserves_every_segment_and_word(self):
        segments = [
            {"text": "First two words", "start": 0.5, "duration": 1.5},
            {"text": "Second segment here", "start": 2.0, "duration": 2.0},
        ]
        record = build_transcript_record("abcdefghijk", segments, "en")
        self.assertEqual(record["segment_count"], 2)
        self.assertEqual(record["word_count"], 6)
        self.assertEqual(record["first_timestamp"], "0:00")
        self.assertEqual(record["last_segment_timestamp"], "0:02")
        self.assertEqual(record["duration"], "0:04")
        self.assertEqual(record["segments"], segments)
        self.assertIn("First two words", record["timestamped_text"])
        self.assertIn("Second segment here", record["timestamped_text"])

    def test_video_sharding_is_stable_and_bounded(self):
        first = video_shard("abcdefghijk", 10)
        self.assertEqual(first, video_shard("abcdefghijk", 10))
        self.assertGreaterEqual(first, 0)
        self.assertLess(first, 10)

    def test_load_selection_ids_returns_exact_ranked_video_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selection.json"
            path.write_text(json.dumps({"videos": [{"video_id": "abcdefghijk"}, {"video_id": "lmnopqrstuv"}]}))
            self.assertEqual(load_selection_ids(path), ["abcdefghijk", "lmnopqrstuv"])


if __name__ == "__main__":
    unittest.main()
