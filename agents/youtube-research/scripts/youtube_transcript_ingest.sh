#!/usr/bin/env bash
set -euo pipefail
SELECTION=/home/USER/.hermes/profiles/youtube-ai-researcher/state/daily_selection/current.json
if [[ ! -s "$SELECTION" ]]; then
  printf '{"status":"no_selection","attempted":0}\n'
  exit 0
fi
exec /home/USER/.hermes/profiles/youtube-ai-researcher/.venv-transcripts/bin/python \
  /home/USER/.hermes/profiles/youtube-ai-researcher/scripts/youtube_transcript_ingest.py \
  --hours 48 --selection-file "$SELECTION" --limit 10 --workers 2 --retry-errors-after-hours 0.5
