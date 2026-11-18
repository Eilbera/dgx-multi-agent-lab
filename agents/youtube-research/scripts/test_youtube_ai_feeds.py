#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import youtube_ai_feeds


class ChannelWatchlistTests(unittest.TestCase):
    def test_load_channels_accepts_ranked_external_watchlist(self):
        payload = {
            "channels": [{
                "name": "Example AI Lab",
                "handle": "ExampleAILab",
                "channel_id": "UC1234567890123456789012",
                "channel_url": "https://www.youtube.com/@ExampleAILab",
                "tier": 1,
                "kind": "primary",
                "score": 91,
                "rank": 7,
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "channels.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            channels = youtube_ai_feeds.load_channels(path)
        self.assertEqual(channels, [{
            "name": "Example AI Lab",
            "handle": "ExampleAILab",
            "id": "UC1234567890123456789012",
            "channel_url": "https://www.youtube.com/@ExampleAILab",
            "tier": 1,
            "kind": "primary",
            "score": 91,
            "rank": 7,
        }])

    def test_colm_official_channel_is_monitored(self):
        matches = [channel for channel in youtube_ai_feeds.CHANNELS if channel["id"] == "UCr4S7b9E82ZGVixVrvC5YjQ"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["name"], "Conference on Language Modeling")
        self.assertEqual(matches[0]["handle"], "colm_conf")
        self.assertEqual(matches[0]["tier"], 1)
        self.assertEqual(matches[0]["kind"], "conference")

    def test_channel_ids_are_unique(self):
        ids = [channel["id"] for channel in youtube_ai_feeds.CHANNELS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_sensational_commentary_sources_are_discovery_only(self):
        discovery_only = {"The AI Grid", "Wes Roth", "The AI Advantage", "MattVidPro AI"}
        actual = {
            channel["name"]: (channel["tier"], channel["kind"])
            for channel in youtube_ai_feeds.CHANNELS
            if channel["name"] in discovery_only
        }
        self.assertTrue(all(value == (3, "discovery-only") for value in actual.values()))

    def test_payload_exposes_reconciled_coverage_counts(self):
        payload = youtube_ai_feeds.build_payload(
            generated_at="2026-08-16T22:00:00Z",
            window_hours=36.0,
            channels_checked=47,
            videos=[{"video_id": "one"}, {"video_id": "two"}],
            errors=[{"channel": "A", "error": "timeout"}],
            unhealthy=[],
        )
        self.assertEqual(payload["coverage"], {
            "configured_channels": 47,
            "live_feed_successes": 46,
            "feed_failures": 1,
            "persisted_candidates": 2,
            "manual_web_additions": 0,
            "deduplicated_discovered": 2,
        })


if __name__ == "__main__":
    unittest.main()
