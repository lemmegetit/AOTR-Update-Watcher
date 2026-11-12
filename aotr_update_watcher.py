#!/usr/bin/env python3
"""
AOTR place update tracker -> Discord webhook (Rate-Limit Protected)

Tracks every place (map) inside the AOTR game and posts a Discord embed when
one or more of them get a new version. Standard library only (Python 3.8+).

  python3 aotr_update_watcher.py            # check forever, every 5 minutes
  python3 aotr_update_watcher.py --once     # single check (cron / GitHub Actions)
  python3 aotr_update_watcher.py --preview  # post a sample alert, change nothing
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
PLACE_ID = os.environ.get("ROBLOX_PLACE_ID", "4658598196")
PLACE_IDS_OVERRIDE = [p.strip() for p in os.environ.get("ROBLOX_PLACE_IDS", "").split(",") if p.strip()]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "aotr_state.json"))

TRACKER_NAME = "AOTR Update Tracker"
EMBED_COLOR = 0x8B5CF6
MAX_LISTED = 10
MAX_WORKERS = 4  # Reduced worker threads to avoid Roblox rate limits (HTTP 429)

USER_AGENT = "aotr-update-watcher/2.2"


def http_json(url, payload=None, retries=4):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as err:
            if err.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # Exponential backoff on rate limits
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def parse_ts(value):
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), value)
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_universe_id(place_id):
    try:
        info = http_json(f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
        return int(info["universeId"])
    except (urllib.error.HTTPError, KeyError, TypeError, ValueError):
        return int(place_id)


def fetch_game(universe_id):
    data = http_json(f"https://games.roblox.com/v1/games?universeIds={universe_id}").get("data", [])
    if not data:
        raise RuntimeError(f"No game found for ID {universe_id}. Check ROBLOX_PLACE_ID.")
    return data[0]


def fetch_icon(universe_id):
    try:
        d = http_json(
            "https://thumbnails.roblox.com/v1/games/icons"
            f"?universe
