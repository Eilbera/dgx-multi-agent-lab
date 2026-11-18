#!/usr/bin/env python3
"""Deterministic quality gate for daily YouTube deep reviews."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


REQUIRED_SECTIONS = (
    "Core argument",
    "How it works / evidence",
    "Caveats / credibility",
    "Why it matters",
    "Verification",
    "Verdict",
)
SECTION_MIN_WORDS = {
    "Core argument": 25,
    "How it works / evidence": 45,
    "Caveats / credibility": 20,
    "Why it matters": 20,
    "Verification": 35,
}
COVERAGE_FIELDS = (
    "configured channels",
    "live feed successes",
    "persisted candidates",
    "manual/web additions",
    "discovered",
    "screened",
    "full transcripts reviewed",
    "top stories presented",
    "transcript failures",
    "feed failures",
)
COVERAGE_LABELS = {
    "configured": "Configured channels",
    "live": "Live feed successes",
    "persisted": "Persisted candidates",
    "manual": "Manual/web additions",
    "discovered": "Discovered",
    "screened": "Screened",
    "reviewed": "Full transcripts reviewed",
    "presented": "Top stories presented",
    "transcript_failures": "Transcript failures",
    "feed_failures": "Feed failures",
}
TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[–-]\s*\d{1,2}:\d{2}(?::\d{2})?)?\b")
WORD_RE = re.compile(r"\b[\w’'-]+\b")
REVIEW_START_RE = re.compile(r"(?m)^\s*(?:[•*-]\s*)?(?:#{1,4}\s*)?\*\*\d+\)|^\s*#{1,4}\s+\d+[.)]")
PUBLISHED_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
YOUTUBE_URL_RE = re.compile(r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]{6,}")
DISCOVERY_ONLY_CHANNELS = {
    "the ai advantage",
    "wes roth",
    "mattvidpro ai",
    "the ai grid",
}


@dataclass(frozen=True)
class Assessment:
    ok: bool
    errors: tuple[str, ...]


def _deep_review_region(text: str) -> str:
    match = re.search(r"(?im)^\s*(?:#{1,4}\s*)?\*\*Deep reviews\*\*\s*$", text)
    if not match:
        return ""
    tail = text[match.end():]
    end = re.search(r"(?im)^\s*(?:#{1,4}\s*)?\*\*(?:Worth knowing|Noise filtered|Coverage)\*\*", tail)
    return tail[: end.start()] if end else tail


def _split_reviews(region: str) -> list[str]:
    starts = [match.start() for match in REVIEW_START_RE.finditer(region)]
    if not starts:
        return []
    starts.append(len(region))
    return [region[starts[i]:starts[i + 1]].strip() for i in range(len(starts) - 1)]


def _timestamp_seconds(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def assess_brief(
    text: str,
    min_words: int = 250,
    max_words: int = 400,
    min_timestamps: int = 3,
    required_reviews: int | None = None,
) -> Assessment:
    errors: list[str] = []
    reviews = _split_reviews(_deep_review_region(text))
    quiet_day = "no material videos warranted deep review today" in text.lower()
    if not reviews and not quiet_day:
        errors.append("No numbered reviews found under **Deep reviews**.")
    if required_reviews is not None and not quiet_day and len(reviews) != required_reviews:
        errors.append(f"Brief must contain exactly {required_reviews} numbered reviews; found {len(reviews)}.")

    for index, review in enumerate(reviews, 1):
        lower = review.lower()
        channel_match = re.search(r"(?im)^\s*[-•*]\s*\*\*Channel:\*\*\s*(.+?)\s*$", review)
        if not channel_match:
            errors.append(f"Review {index} is missing channel metadata (**Channel:**).")
        elif channel_match.group(1).strip().lower() in DISCOVERY_ONLY_CHANNELS:
            errors.append(
                f"Review {index} selects a discovery-only channel for deep review; trace the story to a primary or canonical source."
            )
        published_match = re.search(r"(?im)^\s*[-•*]\s*\*\*Published:\*\*\s*(.+?)\s*$", review)
        if not published_match or not PUBLISHED_DATE_RE.search(published_match.group(1)):
            errors.append(f"Review {index} is missing dated **Published:** metadata in YYYY-MM-DD form.")
        video_match = re.search(r"(?im)^\s*[-•*]\s*\*\*Video:\*\*\s*(.+?)\s*$", review)
        if not video_match or not YOUTUBE_URL_RE.search(video_match.group(1)):
            errors.append(f"Review {index} is missing a valid YouTube **Video:** URL.")
        transcript_match = re.search(
            r"(?im)^\s*[-•*]\s*\*\*Transcript coverage:\*\*\s*Complete transcript reviewed from "
            r"(\d{1,2}:\d{2}(?::\d{2})?) to (\d{1,2}:\d{2}(?::\d{2})?) "
            r"\((\d+) segments\); no portions skipped\.\s*$",
            review,
        )
        if not transcript_match:
            errors.append(
                f"Review {index} is missing a valid **Transcript coverage:** audit confirming the complete transcript was reviewed."
            )
        elif int(transcript_match.group(3)) < 1 or _timestamp_seconds(transcript_match.group(2)) <= _timestamp_seconds(transcript_match.group(1)):
            errors.append(f"Review {index} Transcript coverage has an invalid range or segment count.")
        for section in REQUIRED_SECTIONS:
            if f"**{section.lower()}:**" not in lower:
                errors.append(f"Review {index} is missing required section: {section}.")
        section_names = "|".join(re.escape(name) for name in REQUIRED_SECTIONS)
        for section, minimum in SECTION_MIN_WORDS.items():
            section_match = re.search(
                rf"(?is)\*\*{re.escape(section)}:\*\*(.*?)(?=\n\s*[-•*]?\s*\*\*(?:{section_names}):\*\*|\Z)",
                review,
            )
            if section_match:
                section_text = re.sub(r"https?://\S+", "", section_match.group(1))
                section_words = len(WORD_RE.findall(section_text))
                if section_words < minimum:
                    errors.append(
                        f"Review {index} {section} has {section_words} words; minimum is {minimum} words."
                    )
        words = len(WORD_RE.findall(re.sub(r"https?://\S+", "", review)))
        if words < min_words:
            errors.append(f"Review {index} has {words} words; minimum is {min_words} words.")
        if words > max_words:
            errors.append(f"Review {index} has {words} words; maximum is {max_words} words.")
        timestamp_source = re.sub(r"(?im)^.*\*\*Transcript coverage:\*\*.*$", "", review)
        timestamps = TIMESTAMP_RE.findall(timestamp_source)
        unique_timestamps = {re.sub(r"\s+", "", timestamp) for timestamp in timestamps}
        if len(unique_timestamps) < min_timestamps:
            errors.append(
                f"Review {index} has {len(unique_timestamps)} unique timestamp references; minimum is {min_timestamps} unique timestamps."
            )
        why_match = re.search(
            r"(?is)\*\*Why it matters:\*\*(.*?)(?=\n\s*[-•*]?\s*\*\*(?:Verification|Verdict):\*\*)",
            review,
        )
        why_text = re.sub(r"[*_`]", "", why_match.group(1)).lower() if why_match else ""
        if not re.search(r"\bclassification\s*:\s*(?:new|material update|recycled)\b", why_text):
            errors.append(
                f"Review {index} must classify the development as New, Material update, or Recycled in Why it matters."
            )
        verification_match = re.search(
            r"(?is)\*\*Verification:\*\*(.*?)(?=\n\s*[-•*]?\s*\*\*Verdict:\*\*)",
            review,
        )
        verification_text = verification_match.group(1).lower() if verification_match else ""
        for label in ("Video says", "Primary evidence", "Assessment"):
            if f"**{label.lower()}:**" not in verification_text:
                errors.append(f"Review {index} Verification is missing **{label}:**.")
        verification_urls = re.findall(r"https?://[^\s)\]]+", verification_text)
        primary_urls = [
            url for url in verification_urls
            if "youtube.com" not in url and "youtu.be" not in url
        ]
        if not primary_urls:
            errors.append(f"Review {index} Verification needs at least one non-YouTube primary-source link.")

    action_match = re.search(r"(?im)^\s*(?:#{1,4}\s*)?\*\*Action radar\*\*", text)
    if not action_match:
        errors.append("Brief is missing the **Action radar** section.")
    else:
        action_tail = text[action_match.end():]
        action_end = re.search(
            r"(?im)^\s*(?:#{1,4}\s*)?\*\*(?:Worth knowing|Noise filtered|Coverage)\*\*",
            action_tail,
        )
        action_region = action_tail[: action_end.start()] if action_end else action_tail
        no_action = "no action warranted today" in action_region.lower()
        actions = re.findall(r"(?im)^\s*[-•*]\s*\*\*(Read|Test|Monitor|Ignore):\*\*", action_region)
        if not no_action and not actions:
            errors.append("Action radar needs at least one Read, Test, Monitor, or Ignore item.")
        if len(actions) > 5:
            errors.append(f"Action radar has {len(actions)} items; maximum is 5.")

    coverage_match = re.search(r"(?im)^\s*(?:#{1,4}\s*)?\*\*Coverage\*\*", text)
    coverage = text[coverage_match.end():] if coverage_match else ""
    for field in COVERAGE_FIELDS:
        if field not in coverage.lower():
            errors.append(f"Coverage is missing the {field} count.")

    counts: dict[str, int] = {}
    for key, label in COVERAGE_LABELS.items():
        count_match = re.search(
            rf"(?im)^\s*[-•*]\s*(?:\*\*)?{re.escape(label)}\s*:\s*(?:\*\*)?\s*(\d+)\b",
            coverage,
        )
        if count_match:
            counts[key] = int(count_match.group(1))
        elif label.lower() in coverage.lower():
            errors.append(f"Coverage count for {label} must be numeric.")

    if all(key in counts for key in ("configured", "live", "feed_failures")):
        if counts["live"] + counts["feed_failures"] != counts["configured"]:
            errors.append("Live feed successes plus feed failures must equal configured channels.")
    if all(key in counts for key in ("discovered", "screened")) and counts["screened"] > counts["discovered"]:
        errors.append("Screened videos cannot exceed discovered videos.")
    if all(key in counts for key in ("screened", "reviewed")) and counts["reviewed"] > counts["screened"]:
        errors.append("Full transcripts reviewed cannot exceed screened videos.")
    if all(key in counts for key in ("reviewed", "presented")) and counts["presented"] > counts["reviewed"]:
        errors.append("Top stories presented cannot exceed full transcripts reviewed.")
    if all(key in counts for key in ("persisted", "manual", "discovered")):
        if counts["discovered"] > counts["persisted"] + counts["manual"]:
            errors.append("Discovered videos cannot exceed persisted candidates plus manual/web additions.")
    if "presented" in counts and counts["presented"] != len(reviews):
        errors.append(
            f"Coverage reports {counts['presented']} top stories presented, but {len(reviews)} numbered reviews were found."
        )

    return Assessment(ok=not errors, errors=tuple(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brief", type=Path)
    parser.add_argument("--min-words", type=int, default=250)
    parser.add_argument("--max-words", type=int, default=400)
    parser.add_argument("--min-timestamps", type=int, default=3)
    parser.add_argument("--required-reviews", type=int)
    args = parser.parse_args()

    result = assess_brief(
        args.brief.read_text(encoding="utf-8"),
        min_words=args.min_words,
        max_words=args.max_words,
        min_timestamps=args.min_timestamps,
        required_reviews=args.required_reviews,
    )
    if result.ok:
        print("PASS: deep-review quality gate satisfied")
        return 0
    print("FAIL: deep-review quality gate")
    for error in result.errors:
        print(f"- {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
