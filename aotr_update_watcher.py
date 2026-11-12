#!/usr/bin/env python3
"""
AOTR notifier -> Discord webhook

Watches the Attack on Titan Revolution game on Roblox and posts to Discord when:
  places  - one or more places (maps) get a new version
  status  - the game goes offline / comes back online
  page    - the game's name, description or icon changes
  badges  - the developers add new badges (usually means new content)

Standard library only (Python 3.8+).

  python3 aotr_update_watcher.py            # check forever, every 5 minutes
  python3 aotr_update_watcher.py --once     # single check (cron / GitHub Actions)
  python3 aotr_update_watcher.py --preview  # post a sample of every alert, save nothing

Environment variables:
  DISCORD_WEBHOOK_URL  your Discord webhook URL (required)
  DISCORD_PING         optional, e.g. <@123456789012345678>, added to real alerts
  ROBLOX_FEATURES      which alerts to run         [default: places,status,page,badges]
  ROBLOX_PLACE_ID      any place ID of the game    [default: AOTR]
  ROBLOX_PLACE_IDS     optional comma list; track exactly these places instead
  POLL_SECONDS         seconds between checks      [default: 300]
  STATE_FILE           where state is kept         [default: aotr_state.json]
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
PING = os.environ.get("DISCORD_PING", "").strip()
FEATURES = [f.strip() for f in os.environ.get("ROBLOX_FEATURES", "places,status,page,badges").split(",") if f.strip()]
PLACE_ID = os.environ.get("ROBLOX_PLACE_ID", "4658598196")
PLACE_IDS_OVERRIDE = [p.strip() for p in os.environ.get("ROBLOX_PLACE_IDS", "").split(",") if p.strip()]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "aotr_state.json"))

TRACKER_NAME = "AOTR Update Tracker"
PURPLE, RED, GREEN = 0x8B5CF6, 0xE74C3C, 0x2ECC71
MAX_LISTED = 10
BAD_CHECKS_BEFORE_ALERT = 2      # offline must be seen twice in a row
USER_AGENT = "aotr-update-watcher/3.0"


# ---------------------------------------------------------------- helpers

def http_json(url, payload=None, retries=4):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as err:
            if err.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                try:
                    wait = float(err.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(min(wait, 30) or 2 * (attempt + 1))
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


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clip(text, n=500):
    text = (text or "").strip() or "(empty)"
    return text if len(text) <= n else text[: n - 1] + "…"


def clean(text):
    return (text or "").replace("\r\n", "\n").strip()


def footer(extra=""):
    return {"text": TRACKER_NAME + (f" • {extra}" if extra else "")}


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def post(embed, content=None, ping=True):
    text = " ".join(x for x in (content, PING if ping else "") if x)
    payload = {"username": TRACKER_NAME, "embeds": [embed]}
    if text:
        payload["content"] = text
    http_json(WEBHOOK_URL, payload)


# ---------------------------------------------------------------- roblox

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
    """Returns (url, is_final). A placeholder icon must not count as a change."""
    try:
        d = http_json(
            "https://thumbnails.roblox.com/v1/games/icons"
            f"?universeIds={universe_id}&returnPolicy=PlaceHolder&size=256x256&format=Png"
        )["data"][0]
        return d.get("imageUrl"), d.get("state") == "Completed"
    except Exception:
        return None, False


def fetch_playable(universe_id):
    try:
        d = http_json(f"https://games.roblox.com/v1/games/multiget-playability-status?universeIds={universe_id}")
        return bool(d[0].get("isPlayable", True))
    except Exception:
        return True  # unknown: never claim the game is down because of this


def fetch_badges(universe_id):
    page = http_json(
        f"https://badges.roblox.com/v1/universes/{universe_id}/badges"
        "?limit=100&sortBy=DateCreated&sortOrder=Desc"
    )
    return [
        {"id": str(b["id"]), "name": b.get("name") or str(b["id"]), "description": b.get("description") or ""}
        for b in page.get("data", [])
        if b.get("enabled", True)
    ]


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
    d = http_json(f"https://economy.roblox.com/v2/assets/{place['id']}/details")
    return place["id"], {"name": place["name"] or d.get("Name") or place["id"], "updated": d["Updated"]}


def gather_places(universe_id):
    """Read every place one at a time; retry the ones Roblox rate-limited so a
    single update is not split across two alerts."""
    places = list_places(universe_id)
    current, missing = {}, list(places)
    for rnd in range(3):
        failed = []
        for p in missing:
            try:
                pid, info = fetch_place(p)
                current[pid] = info
            except Exception as exc:
                failed.append(p)
                print(f"Place {p['id']} failed ({exc}); will retry", file=sys.stderr)
            time.sleep(0.4)
        missing = failed
        if not missing:
            break
        time.sleep(8 * (rnd + 1))
    for p in missing:
        print(f"Skipping place {p['id']} this run", file=sys.stderr)
    if not current:
        raise RuntimeError("Could not read any places from Roblox.")
    return len(places), current


# ---------------------------------------------------------------- embeds

def places_embed(game, changed_ids, current, total, icon):
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
        "color": PURPLE,
        "fields": [
            {"name": "Most recent change detected", "value": f"<t:{epoch}:F> • <t:{epoch}:R>"},
            {"name": "Place ID", "value": f"`{newest}`"},
            {"name": "Places Updated", "value": "\n".join(lines)},
        ],
        "footer": footer(f"{total} places tracked"),
        "timestamp": now_iso(),
    }
    if icon:
        embed["thumbnail"] = {"url": icon}
    return embed


def status_embed(game, down, playable, since=None, icon=None):
    if down:
        embed = {
            "title": game["name"],
            "description": "**Servers look offline.**",
            "color": RED,
            "fields": [
                {"name": "Players online", "value": f"{game.get('playing', 0):,}", "inline": True},
                {"name": "Roblox says playable", "value": "yes" if playable else "no", "inline": True},
            ],
        }
    else:
        fields = [{"name": "Players online", "value": f"{game.get('playing', 0):,}", "inline": True}]
        if since:
            mins = max(1, int((datetime.now(timezone.utc) - parse_ts(since)).total_seconds() // 60))
            fields.append({"name": "Seen offline for about", "value": f"{mins} min", "inline": True})
        embed = {"title": game["name"], "description": "**Back online.**", "color": GREEN, "fields": fields}
    embed["footer"] = footer()
    embed["timestamp"] = now_iso()
    if icon:
        embed["thumbnail"] = {"url": icon}
    return embed


def page_embed(game, changes, icon):
    fields = []
    if "name" in changes:
        old, new = changes["name"]
        fields.append({"name": "Name", "value": f"{clip(old, 200)}\n→ {clip(new, 200)}"})
    if "description" in changes:
        old, new = changes["description"]
        fields.append({"name": "Description before", "value": clip(old, 700)})
        fields.append({"name": "Description now", "value": clip(new, 700)})
    if "icon" in changes:
        fields.append({"name": "Icon", "value": "The game icon changed."})
    embed = {
        "title": game["name"],
        "description": "**Game page updated.**",
        "color": PURPLE,
        "fields": fields,
        "footer": footer(),
        "timestamp": now_iso(),
    }
    if icon:
        embed["thumbnail"] = {"url": icon}
    return embed


def badges_embed(game, new, icon):
    n = len(new)
    lines = [f"• **{clip(b['name'], 80)}**" + (f" — {clip(b['description'], 120)}" if b["description"] else "")
             for b in new[:MAX_LISTED]]
    if n > MAX_LISTED:
        lines.append(f"…and {n - MAX_LISTED} more")
    embed = {
        "title": game["name"],
        "description": f"**{n} new badge{'s' if n != 1 else ''}.**",
        "color": PURPLE,
        "fields": [{"name": "New badges", "value": "\n".join(lines)}],
        "footer": footer(),
        "timestamp": now_iso(),
    }
    if icon:
        embed["thumbnail"] = {"url": icon}
    return embed


# ---------------------------------------------------------------- features
# Each feature only changes `state` after its Discord post succeeded, so a
# failed post is simply retried on the next run.

def run_places(ctx):
    state, game = ctx["state"], ctx["game"]
    total, current = gather_places(ctx["universe_id"])
    now_seen = {pid: v["updated"] for pid, v in current.items()}
    saved = state.get("places")
    if saved is None:
        post({"title": f"Now watching {game['name']}",
              "description": f"Tracking {total} places. Alerts enabled: {', '.join(FEATURES)}.",
              "color": PURPLE}, ping=False)
        state["places"] = now_seen
        return
    changed = [pid for pid, ts in now_seen.items() if pid in saved and saved[pid] != ts]
    if changed:
        post(places_embed(game, changed, current, total, ctx["icon"]))
        print(f"Places updated: {', '.join(changed)}")
    state["places"] = {**saved, **now_seen}


def run_status(ctx):
    state, game = ctx["state"], ctx["game"]
    s = state.get("status") or {"down": False, "bad": 0}
    playable = fetch_playable(ctx["universe_id"])
    bad = game.get("playing", 0) == 0 or not playable
    if bad:
        s["bad"] = s.get("bad", 0) + 1
        if not s["down"] and s["bad"] >= BAD_CHECKS_BEFORE_ALERT:
            post(status_embed(game, True, playable, icon=ctx["icon"]))
            s["down"], s["since"] = True, now_iso()
            print("Game looks offline")
    else:
        s["bad"] = 0
        if s["down"]:
            post(status_embed(game, False, True, since=s.get("since"), icon=ctx["icon"]))
            s["down"] = False
            print("Game is back online")
    state["status"] = s


def run_page(ctx):
    state, game = ctx["state"], ctx["game"]
    snap = {"name": game.get("name") or "", "description": clean(game.get("description"))}
    if ctx["icon_final"] and ctx["icon"]:
        snap["icon"] = ctx["icon"]
    saved = state.get("page")
    if saved is None:
        state["page"] = snap
        return
    changes = {k: (saved[k], snap[k]) for k in snap if k in saved and saved[k] != snap[k]}
    if changes:
        post(page_embed(game, changes, ctx["icon"]))
        print(f"Game page changed: {', '.join(changes)}")
    state["page"] = {**saved, **snap}


def run_badges(ctx):
    state, game = ctx["state"], ctx["game"]
    badges = fetch_badges(ctx["universe_id"])
    ids = [b["id"] for b in badges]
    saved = state.get("badges")
    if saved is None:
        state["badges"] = ids
        return
    known = set(saved)
    new = [b for b in badges if b["id"] not in known]
    if new:
        post(badges_embed(game, new, ctx["icon"]))
        print(f"New badges: {', '.join(b['name'] for b in new)}")
    state["badges"] = list(dict.fromkeys(ids + saved))[:300]


STEPS = {"places": run_places, "status": run_status, "page": run_page, "badges": run_badges}


def run_preview(ctx):
    game, icon = ctx["game"], ctx["icon"]
    total, current = gather_places(ctx["universe_id"])
    newest = max(current, key=lambda pid: parse_ts(current[pid]["updated"]))
    samples = {"places": lambda: places_embed(game, [newest], current, total, icon)}
    samples["status"] = lambda: status_embed(game, True, False, icon=icon)
    samples["page"] = lambda: page_embed(game, {"description": ("(the old description)", clean(game.get("description")))}, icon)
    samples["badges"] = lambda: badges_embed(game, fetch_badges(ctx["universe_id"])[:3], icon)
    for name in FEATURES:
        if name in samples:
            post(samples[name](), content=f"Sample '{name}' alert (nothing was saved):", ping=False)
            time.sleep(1)
    print("Preview posted")


def check_once(universe_id, preview=False):
    game = fetch_game(universe_id)
    icon, icon_final = fetch_icon(universe_id)
    ctx = {"universe_id": universe_id, "game": game, "icon": icon, "icon_final": icon_final, "state": load_state()}

    if preview:
        run_preview(ctx)
        return

    enabled = [f for f in FEATURES if f in STEPS]
    failures = 0
    for name in enabled:
        try:
            STEPS[name](ctx)
        except Exception as exc:
            failures += 1
            print(f"{name} check failed: {exc}", file=sys.stderr)
    save_state(ctx["state"])
    if enabled and failures == len(enabled):
        raise RuntimeError("Every check failed this run.")
    print(f"Checked: {', '.join(enabled)}")


def main():
    parser = argparse.ArgumentParser(description="AOTR notifier")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--preview", action="store_true", help="post a sample of each alert and exit")
    args = parser.parse_args()

    if not WEBHOOK_URL.startswith("https://"):
        sys.exit("Set DISCORD_WEBHOOK_URL to your Discord webhook URL first.")

    universe_id = resolve_universe_id(PLACE_ID)
    single = args.once or args.preview

    while True:
        try:
            check_once(universe_id, preview=args.preview)
        except Exception as exc:
            print(f"Check failed: {exc}", file=sys.stderr)
            if single:
                sys.exit(1)
        if single:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
