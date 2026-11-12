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
            f"?universeIds={universe_id}&returnPolicy=PlaceHolder&size=256x256&format=Png"
        )
        return d["data"][0].get("imageUrl")
    except Exception:
        return None


def list_places(universe_id):
    if PLACE_IDS_OVERRIDE:
        return [{"id": p, "name": None} for p in PLACE_IDS_OVERRIDE]
    places, cursor = [], None
    while True:
        url = f"https://develop.roblox.com/v1/universes/{universe_id}/places?limit=100&sortOrder=Asc"
        if cursor:
            url += f"&cursor={cursor}"
        page = http_json(url)
        for p in page.get("data", []):
            places.append({"id": str(p["id"]), "name": p.get("name")})
        cursor = page.get("nextPageCursor")
        if not cursor:
            return places


def fetch_place(place):
    time.sleep(0.1)  # Micro delay per request to avoid hitting 429 rate limits
    pid = place["id"]
    d = http_json(f"https://economy.roblox.com/v2/assets/{pid}/details")
    name = d.get("Name")
    updated = d["Updated"]
    return pid, {"name": place["name"] or name or pid, "updated": updated}


def gather(universe_id):
    places = list_places(universe_id)
    current = {}
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_place, p): p for p in places}
        for future in as_completed(futures):
            p_info = futures[future]
            try:
                pid, data = future.result()
                current[pid] = data
            except Exception as exc:
                print(f"Skipping place {p_info['id']}: {exc}", file=sys.stderr)

    if not current:
        raise RuntimeError("Could not read any places from Roblox.")
    return len(places), current


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(places):
    STATE_FILE.write_text(json.dumps({"places": places}, indent=2))


def post_discord(payload):
    http_json(WEBHOOK_URL, {"username": TRACKER_NAME, **payload})


def build_update_embed(game, changed_ids, current, total, icon):
    changed = sorted(changed_ids, key=lambda pid: parse_ts(current[pid]["updated"]), reverse=True)
    newest = changed[0]
    epoch = int(parse_ts(current[newest]["updated"]).timestamp())
    n = len(changed)
    lines = [f"• {current[pid]['name']} (`{pid}`)" for pid in changed[:MAX_LISTED]]
    if n > MAX_LISTED:
        lines.append(f"…and {n - MAX_LISTED} more")
    
    embed = {
        "title": game["name"],
        "description": f"**{n} place{'s' if n != 1 else ''} updated.**",
        "color": EMBED_COLOR,
        "fields": [
            {"name": "Most recent change detected", "value": f"<t:{epoch}:F> • <t:{epoch}:R>"},
            {"name": "Place ID", "value": f"`{newest}`"},
            {"name": "Places Updated", "value": "\n".join(lines)},
        ],
        "footer": {"text": f"{TRACKER_NAME} • {total} places tracked"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if icon:
        embed["thumbnail"]
