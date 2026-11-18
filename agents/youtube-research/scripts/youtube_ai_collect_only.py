#!/usr/bin/env python3
"""Silent four-hour ingestion pass for the YouTube AI monitor."""
import subprocess
import sys

SCRIPT = "/home/USER/.hermes/profiles/youtube-ai-researcher/scripts/youtube_ai_feeds.py"
result = subprocess.run([sys.executable, SCRIPT, "--collect-only"], stdout=subprocess.DEVNULL)
raise SystemExit(result.returncode)
