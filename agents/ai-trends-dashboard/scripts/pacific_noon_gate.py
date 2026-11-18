#!/usr/bin/env python3
"""Emit a stable key that changes only at Pacific noon."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("America/Los_Angeles"))
noon_key = now.date() if now.hour >= 12 else (now - timedelta(days=1)).date()
print(f"pacific-noon={noon_key.isoformat()}")
