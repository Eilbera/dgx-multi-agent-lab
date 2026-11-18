#!/usr/bin/env python3
import json
import unittest
from pathlib import Path

WATCHLIST = Path("/home/USER/.hermes/profiles/youtube-ai-researcher/research/youtube_top500_verified.json")


class Top500WatchlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(WATCHLIST.read_text(encoding="utf-8"))
        cls.channels = cls.payload["channels"]

    def test_contains_exactly_500_ranked_channels(self):
        self.assertEqual(len(self.channels), 500)
        self.assertEqual([row["rank"] for row in self.channels], list(range(1, 501)))

    def test_channel_ids_are_unique_and_feeds_verified(self):
        ids = [row["channel_id"] for row in self.channels]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(row["feed_verified"] for row in self.channels))
        self.assertTrue(all(row["feed_url"].endswith(row["channel_id"]) for row in self.channels))

    def test_every_channel_has_monitoring_metadata(self):
        required = {
            "name", "channel_id", "channel_url", "category", "language_or_region",
            "kind", "tier", "score", "feed_verified", "verification_notes",
        }
        for row in self.channels:
            self.assertFalse(required - set(row), f"{row.get('name')} missing {required - set(row)}")
            self.assertIn(row["tier"], (1, 2, 3))
            self.assertGreaterEqual(row["score"], 0)
            self.assertLessEqual(row["score"], 100)

    def test_original_verified_watchlist_is_preserved(self):
        import youtube_ai_feeds
        configured = {row["channel_id"] for row in self.channels}
        expected = {
            "UCXZCJLdBC09xxGZ6gcdrc6A",  # OpenAI
            "UCrDwWp7EBBv4NwvScIpBDOA",  # Anthropic
            "UCP7jMXSY2xbc3KCAE0MHQ-A",  # Google DeepMind
            "UCr4S7b9E82ZGVixVrvC5YjQ",  # COLM
        }
        self.assertTrue(expected <= configured)


if __name__ == "__main__":
    unittest.main()
