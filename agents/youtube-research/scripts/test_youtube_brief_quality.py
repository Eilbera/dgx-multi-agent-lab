import tempfile
import unittest
from pathlib import Path

from youtube_brief_quality import assess_brief


RICH_REVIEW = """**Deep reviews**

• **1) Example video**
- **Channel:** Example Research Lab
- **Published:** 2026-08-16 14:00 UTC
- **Video:** https://www.youtube.com/watch?v=abcdefghijk
- **Transcript coverage:** Complete transcript reviewed from 00:00 to 31:55 (486 segments); no portions skipped.
- **Core argument:** The speaker argues that reliability comes from orchestration, not a stronger base model. Retrieval supplies context, tools perform bounded operations, and an evaluator checks intermediate results. The contribution is an inspectable recovery loop that addresses common single-prompt failures rather than a claim of unrestricted autonomy.
- **How it works / evidence:** A request becomes a plan, then retrieval supplies documents for the current step at **01:20–02:10**. A permissioned tool executes the operation and records its output. At **08:40–10:15**, an evaluator compares that output with task-specific criteria; failed checks return the error and trace to planning instead of continuing. The demo at **22:00–24:30** follows retrieval, a deliberately failed call, rejection, and recovery. It shows the mechanism clearly and reports fewer unrecovered errors than a single-pass baseline, but provides no broad benchmark or independent replication.
- **Caveats / credibility:** The vendor chose a narrow demonstration with crisp evaluator rules. It does not measure false approvals, recovery cost, latency, bad retrieval, or stronger agent baselines. Safety testing and write-permission stress tests are also missing, so the demo supports the workflow mechanics more strongly than production reliability.
- **Why it matters:** Classification: **New**. The design makes failures observable and recoverable. Teams can test it now on bounded workflows, but should retain approval for consequential writes and measure retries, cost, and false approvals. The video does not establish that the loop scales to open-ended or long-running autonomy.
- **Verification:** **Video says:** the evaluator loop catches failed intermediate steps. **Primary evidence:** the [official repository](https://github.com/example/project) exposes the planner, permission boundary, evaluator prompts, and recovery path, supporting the architectural description. It does not reproduce the headline reliability result or supply an independent benchmark. **Assessment:** the mechanism is credible, but performance remains vendor-reported and the missing baseline, safety, and cost measurements materially limit the conclusion.
- **Verdict:** Worth skimming. Most useful range: **08:40–24:30**.

**Action radar**
- **Read:** The exact evaluation report because it contains the full baseline comparison.
- **Test:** The linked repository in a sandbox before considering production use.

**Coverage**
- Configured channels: 46
- Live feed successes: 46
- Persisted candidates: 20
- Manual/web additions: 0
- Discovered: 20
- Screened: 20
- Full transcripts reviewed: 20
- Top stories presented: 1
- Transcript failures: 0
- Feed failures: 0
"""

QUIET_BRIEF = """**Bottom line**
No consequential new AI video cleared the evidence and novelty thresholds today.

**Deep reviews**
No material videos warranted deep review today.

**Action radar**
- No action warranted today.

**Coverage**
- Configured channels: 47
- Live feed successes: 47
- Persisted candidates: 8
- Manual/web additions: 0
- Discovered: 8
- Screened: 8
- Full transcripts reviewed: 0
- Top stories presented: 0
- Transcript failures: 0
- Feed failures: 0
"""


class BriefQualityTests(unittest.TestCase):
    def test_accepts_substantive_structured_review(self):
        result = assess_brief(RICH_REVIEW)
        self.assertTrue(result.ok, result.errors)

    def test_rejects_shallow_core_explanation(self):
        shallow = RICH_REVIEW.replace(
            "The speaker argues that reliability comes from orchestration, not a stronger base model. Retrieval supplies context, tools perform bounded operations, and an evaluator checks intermediate results. The contribution is an inspectable recovery loop that addresses common single-prompt failures rather than a claim of unrestricted autonomy.",
            "A model was released and the speaker says orchestration improves it.",
        )
        result = assess_brief(shallow)
        self.assertFalse(result.ok)
        self.assertTrue(any("Core argument" in error and "words" in error for error in result.errors))

    def test_rejects_overlong_video_summary(self):
        overlong = RICH_REVIEW.replace(
            "The contribution is an inspectable recovery loop",
            " ".join(["Additional repetitive detail"] * 80) + ". The contribution is an inspectable recovery loop",
        )
        result = assess_brief(overlong)
        self.assertFalse(result.ok)
        self.assertTrue(any("maximum is" in error.lower() for error in result.errors))

    def test_can_require_exactly_ten_top_stories(self):
        result = assess_brief(RICH_REVIEW, required_reviews=10)
        self.assertFalse(result.ok)
        self.assertTrue(any("exactly 10" in error.lower() for error in result.errors))

    def test_rejects_review_without_complete_transcript_audit(self):
        result = assess_brief(
            RICH_REVIEW.replace(
                "- **Transcript coverage:** Complete transcript reviewed from 00:00 to 31:55 (486 segments); no portions skipped.\n",
                "",
            )
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("Transcript coverage" in error for error in result.errors))

    def test_accepts_explicit_quiet_day_without_padding(self):
        result = assess_brief(QUIET_BRIEF)
        self.assertTrue(result.ok, result.errors)

    def test_rejects_zero_reviews_without_quiet_day_declaration(self):
        result = assess_brief(QUIET_BRIEF.replace("No material videos warranted deep review today.", "Nothing selected."))
        self.assertFalse(result.ok)
        self.assertTrue(any("no numbered reviews" in error.lower() for error in result.errors))

    def test_rejects_shallow_review_without_required_depth(self):
        shallow = """**Deep reviews**
• **1) Example**
- **Core argument:** A model was released.
- **Verdict:** Worth skimming.

**Coverage**
- Discovered: 2
"""
        result = assess_brief(shallow)
        self.assertFalse(result.ok)
        self.assertTrue(any("required section" in error for error in result.errors))
        self.assertTrue(any("timestamps" in error for error in result.errors))
        self.assertTrue(any("words" in error for error in result.errors))

    def test_rejects_missing_coverage_counts(self):
        result = assess_brief(RICH_REVIEW.replace("- Screened: 20\n", ""))
        self.assertFalse(result.ok)
        self.assertTrue(any("screened" in error.lower() for error in result.errors))

    def test_rejects_coverage_without_feed_provenance(self):
        result = assess_brief(RICH_REVIEW.replace("- Live feed successes: 46\n", ""))
        self.assertFalse(result.ok)
        self.assertTrue(any("live feed successes" in error.lower() for error in result.errors))

    def test_rejects_nonnumeric_coverage_count(self):
        result = assess_brief(RICH_REVIEW.replace("- Screened: 20", "- Screened: all"))
        self.assertFalse(result.ok)
        self.assertTrue(any("numeric" in error.lower() for error in result.errors))

    def test_rejects_impossible_coverage_funnel(self):
        result = assess_brief(RICH_REVIEW.replace("- Full transcripts reviewed: 20", "- Full transcripts reviewed: 21"))
        self.assertFalse(result.ok)
        self.assertTrue(any("cannot exceed screened" in error.lower() for error in result.errors))

    def test_rejects_feed_totals_that_do_not_reconcile(self):
        result = assess_brief(RICH_REVIEW.replace("- Feed failures: 0", "- Feed failures: 2"))
        self.assertFalse(result.ok)
        self.assertTrue(any("must equal configured channels" in error.lower() for error in result.errors))

    def test_rejects_review_without_novelty_classification(self):
        result = assess_brief(RICH_REVIEW.replace("Classification: **New**. ", ""))
        self.assertFalse(result.ok)
        self.assertTrue(any("new, material update, or recycled" in error.lower() for error in result.errors))

    def test_rejects_verification_without_claim_evidence_assessment_split(self):
        result = assess_brief(RICH_REVIEW.replace("**Primary evidence:**", "**Evidence:**"))
        self.assertFalse(result.ok)
        self.assertTrue(any("primary evidence" in error.lower() for error in result.errors))

    def test_rejects_verification_without_primary_source_link(self):
        result = assess_brief(RICH_REVIEW.replace("https://github.com/example/project", ""))
        self.assertFalse(result.ok)
        self.assertTrue(any("primary-source link" in error.lower() for error in result.errors))

    def test_rejects_repeated_timestamp_padded_as_three_references(self):
        padded = RICH_REVIEW
        for timestamp in ("01:20–02:10", "08:40–10:15", "22:00–24:30", "08:40–24:30"):
            padded = padded.replace(timestamp, "01:20")
        result = assess_brief(padded)
        self.assertFalse(result.ok)
        self.assertTrue(any("unique timestamp" in error.lower() for error in result.errors))

    def test_rejects_review_without_source_metadata(self):
        result = assess_brief(RICH_REVIEW.replace("- **Channel:** Example Research Lab\n", ""))
        self.assertFalse(result.ok)
        self.assertTrue(any("channel metadata" in error.lower() for error in result.errors))

    def test_rejects_discovery_only_channel_as_deep_review(self):
        result = assess_brief(RICH_REVIEW.replace("Example Research Lab", "Wes Roth"))
        self.assertFalse(result.ok)
        self.assertTrue(any("discovery-only" in error.lower() for error in result.errors))

    def test_rejects_missing_action_radar(self):
        start = RICH_REVIEW.index("**Action radar**")
        end = RICH_REVIEW.index("**Coverage**")
        result = assess_brief(RICH_REVIEW[:start] + RICH_REVIEW[end:])
        self.assertFalse(result.ok)
        self.assertTrue(any("action radar" in error.lower() for error in result.errors))

    def test_accepts_explicit_no_action_decision(self):
        start = RICH_REVIEW.index("**Action radar**")
        end = RICH_REVIEW.index("**Coverage**")
        no_action = RICH_REVIEW[:start] + "**Action radar**\n- No action warranted today.\n\n" + RICH_REVIEW[end:]
        result = assess_brief(no_action)
        self.assertTrue(result.ok, result.errors)


if __name__ == "__main__":
    unittest.main()
