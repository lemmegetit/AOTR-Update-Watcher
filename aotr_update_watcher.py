@@ -1,6 +1,6 @@
#!/usr/bin/env python3
"""
AOTR place update tracker -> Discord webhook (Optimized)
AOTR place update tracker -> Discord webhook (Rate-Limit Protected)

Tracks every place (map) inside the AOTR game and posts a Discord embed when
one or more of them get a new version. Standard library only (Python 3.8+).
@@ -31,12 +31,12 @@
TRACKER_NAME = "AOTR Update Tracker"
EMBED_COLOR = 0x8B5CF6
MAX_LISTED = 10
MAX_WORKERS = 10  # Parallel threads for fast Roblox API checks
MAX_WORKERS = 4  # Reduced worker threads to avoid Roblox rate limits (HTTP 429)

USER_AGENT = "aotr-update-watcher/2.1"
USER_AGENT = "aotr-update-watcher/2.2"


def http_json(url, payload=None, retries=3):
def http_json(url, payload=None, retries=4):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if payload is not None:
@@ -51,12 +51,12 @@
                return json.loads(body) if body else None
        except urllib.error.HTTPError as err:
            if err.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1 + attempt)
                time.sleep(2 * (attempt + 1))  # Exponential backoff on rate limits
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(1 + attempt)
                time.sleep(2 * (attempt + 1))
                continue
            raise

@@ -111,6 +111,7 @@


def fetch_place(place):
    time.sleep(0.1)  # Micro delay per request to avoid hitting 429 rate limits
    pid = place["id"]
    d = http_json(f"https://economy.roblox.com/v2/assets/{pid}/details")
    name = d.get("Name")
@@ -122,120 +123,119 @@
    places = list_places(universe_id)
    current = {}

    # Fetch details for all places concurrently in parallel
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
        embed["thumbnail"] = {"url": icon}
    return embed


def check_once(universe_id, preview=False):
    game = fetch_game(universe_id)
    total, current = gather(universe_id)

    if preview:
        newest = max(current, key=lambda pid: parse_ts(current[pid]["updated"]))
        post_discord({
            "content": "Preview of the update alert (sample data, nothing was saved):",
            "embeds": [build_update_embed(game, [newest], current, total, fetch_icon(universe_id))],
        })
        print("Preview posted")
        return

    saved = load_state().get("places")
    now_seen = {pid: v["updated"] for pid, v in current.items()}

    if saved is None:
        post_discord({"embeds": [{
            "title": f"Now watching {game['name']}",
            "description": f"Tracking {total} places. I'll post here when any of them update.",
            "color": EMBED_COLOR,
        }]})
        save_state(now_seen)
        print(f"Baseline stored for {len(now_seen)} places")
        return

    changed = [pid for pid, ts in now_seen.items() if pid in saved and saved[pid] != ts]
    if changed:
        post_discord({"embeds": [build_update_embed(game, changed, current, total, fetch_icon(universe_id))]})
        print(f"Updated places: {', '.join(changed)}")
    else:
        print("No change")
    save_state({**saved, **now_seen})


def main():
    parser = argparse.ArgumentParser(description="AOTR place update tracker")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--preview", action="store_true", help="post a sample alert and exit")
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
