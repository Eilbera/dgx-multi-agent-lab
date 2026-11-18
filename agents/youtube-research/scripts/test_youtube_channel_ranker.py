#!/usr/bin/env python3
import unittest

from youtube_channel_ranker import infer_kind, score


class ChannelRankingTests(unittest.TestCase):
    def test_search_query_wording_does_not_make_channel_academic(self):
        row = {
            "name": "Generic Creator",
            "category": "search-discovered",
            "kind": "unclassified",
            "query_hits": ["machine learning research seminar"],
        }
        self.assertEqual(infer_kind(row), "analysis")

    def test_curated_kind_labels_are_normalized(self):
        self.assertEqual(infer_kind({"kind": "official lab", "name": "Lab", "category": "AI research"}), "primary")
        self.assertEqual(
            infer_kind({"kind": "technical_educator_or_explainer", "name": "Teacher", "category": "education"}),
            "education",
        )
        self.assertEqual(
            infer_kind({"kind": "research_interview", "name": "Research Podcast", "category": "interviews"}),
            "interview-analysis",
        )

    def test_search_only_unverified_candidate_cannot_enter_tier_one(self):
        row = {
            "name": "Generic AI Creator",
            "category": "search-discovered",
            "kind": "unclassified",
            "official_status": "unverified",
            "source_files": ["candidates_search.json"],
            "query_hits": ["AI research"] * 8,
            "feed_titles": ["AI machine learning agent research"] * 15,
            "newest_published": "2026-08-19T00:00:00+00:00",
        }
        ranked = score(row)
        self.assertLessEqual(ranked["score"], 59)
        self.assertEqual(ranked["tier"], 3)


if __name__ == "__main__":
    unittest.main()
