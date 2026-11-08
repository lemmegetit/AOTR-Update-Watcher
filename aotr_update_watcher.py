#!/usr/bin/env python3
"""
AOTR update watcher -> Discord webhook

Polls Roblox for the game's "last updated" time and posts to your Discord
webhook whenever it changes. Standard library only (Python 3.8+).

Setup:
  1. Discord: Server Settings > Integrations > Webhooks > New Webhook > Copy URL
  2. export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
  3. python3 aotr_update_watcher.py          # runs forever, checks every 5 min
     python3 aotr_update_watcher.py --once   # single check (for cron / CI)

Optional environment variables:
  ROBLOX_PLACE_ID  place (or universe) ID of the game   [default: AOTR]
  POLL_SECONDS     seconds between checks                [default: 300]
  STATE_FILE       where the last-seen update is stored  [default: aotr_state.json]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
PLACE_ID = os.environ.get("ROBLOX_PLACE_ID", "4658598196")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "aotr_state.json"))

# Discord's Cloudflare layer rejects the default Python user agent, so set one.
USER_AGENT = "aotr-update-watcher/1.0"


def http_json(url, payload=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read()
        return json.loads(body) if body else None  # Discord replies 204 (empty)


def resolve_universe_id(place_id):
    """Roblox tracks updates per universe (the whole game), not per place."""
    try:
        info = http_json(
            f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"
        )
        return int(info["universeId"])
    except (urllib.error.HTTPError, KeyError, TypeError, ValueError):
        return int(place_id)  # maybe a universe ID was supplied directly


def fetch_game(universe_id):
    data = http_json(
        f"https://games.roblox.com/v1/games?universeIds={universe_id}"
    ).get("data", [])
    if not data:
        raise RuntimeError(
            f"No game found for ID {universe_id}. Check ROBLOX_PLACE_ID."
        )
    return data[0]


def parse_ts(value):
    """Roblox timestamps have 1-7 fractional digits; normalise before parsing."""
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), value)
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def post_discord(embed):
    http_json(
        WEBHOOK_URL,
        {"username": "AOTR Update Watcher", "embeds": [embed]},
    )


def build_update_embed(game, previous_updated):
    updated = parse_ts(game["updated"])
    epoch = int(updated.timestamp())
    fields = [
        {"name": "Published", "value": f"<t:{epoch}:F>", "inline": True},
        {"name": "Playing now", "value": f"{game.get('playing', 0):,}", "inline": True},
    ]
    if previous_updated:
        prev_epoch = int(parse_ts(previous_updated).timestamp())
        fields.append(
            {"name": "Previous update", "value": f"<t:{prev_epoch}:R>", "inline": True}
        )
    return {
        "title": f"{game['name']} just updated",
        "url": f"https://www.roblox.com/games/{game.get('rootPlaceId', PLACE_ID)}",
        "description": f"A new version was published <t:{epoch}:R>.",
        "color": 0x2ECC71,
        "fields": fields,
        "timestamp": updated.isoformat(),
    }


def check_once(universe_id):
    game = fetch_game(universe_id)
    current = game["updated"]
    state = load_state()
    previous = state.get("updated")

    if previous is None:
        # First run: confirm the webhook works and remember the baseline.
        post_discord(
            {
                "title": f"Now watching {game['name']}",
                "description": "I'll post here when a new update goes live.",
                "color": 0x3498DB,
            }
        )
        save_state({"updated": current})
        print(f"Baseline stored: {current}")
    elif current != previous:
        post_discord(build_update_embed(game, previous))
        save_state({"updated": current})  # saved only after a successful post
        print(f"Update detected: {previous} -> {current}")
    else:
        print(f"No change ({current})")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--once", action="store_true", help="check once and exit")
    args = parser.parse_args()

    if not WEBHOOK_URL.startswith("https://"):
        sys.exit("Set DISCORD_WEBHOOK_URL to your Discord webhook URL first.")

    universe_id = resolve_universe_id(PLACE_ID)

    while True:
        try:
            check_once(universe_id)
        except Exception as exc:  # keep the watcher alive through network blips
            print(f"Check failed: {exc}", file=sys.stderr)
            if args.once:
                sys.exit(1)
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
