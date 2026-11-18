#!/usr/bin/env python3
import sys
from youtube_transcript_review_queue import main

sys.argv.extend(["--shard", "6", "--shards", "10", "--limit", "4", "--hours", "48"])
raise SystemExit(main())
