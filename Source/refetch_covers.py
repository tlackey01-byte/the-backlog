"""Re-fetch game covers from SteamGridDB at full 600x900 and re-bake them sharp.

Why: the covers baked into master_games_final.json are 130x195 JPEGs (a leftover from when
every cover was inlined into the page as base64 and total page weight was the constraint).
The detail hero shows a cover at 200x267 CSS px on desktop and 120x160 on a phone, which is
400x533 real pixels on a 2x display and 360x480 on a phone at 3x -- so a 130px-wide source
got upscaled 3x and looked soft. SteamGridDB serves 600x900 art; this pulls that and
re-encodes at 400px wide, which covers both of those with room to spare. Raising the hero
sizes past 200 CSS px means raising --width to match, or the blur comes back.

Output is 3:4, not the source 2:3: every cover box in the UI is 3:4 and object-fit:cover
already crops the art to that, so cropping here matches what you see and saves ~11% weight.

Matching mirrors worker/src/index.js `details()`: SteamGridDB autocomplete, keep only
candidates whose normalized name equals the game's. A game with no exact match keeps its
existing cover and is listed in the report for review -- guessing silently would swap in art
for the wrong game. Resolved ids are cached to --ids-file so later runs skip the search.

  # dry run: 30 evenly spaced games, images + report into a preview dir, master untouched
  python Source/refetch_covers.py --sample 30 --out <dir>

  # full run, writing new covers back into the master JSON (backs it up first)
  python Source/refetch_covers.py --apply

Needs: Pillow (WebP) and Source/steamgriddb_api_key.txt.
"""

import argparse
import base64
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(HERE, "master_games_final.json")
KEY_FILE = os.path.join(HERE, "steamgriddb_api_key.txt")
IDS_FILE = os.path.join(HERE, "cover_sources.json")
SGDB = "https://www.steamgriddb.com/api/v2"
UA = "the-backlog-cover-refresh"


def norm(s):
    """Same normalization the worker uses, so both agree on what an exact match is."""
    s = re.sub(r"[™®©]", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def http(url, key=None, tries=4):
    """GET with backoff. SteamGridDB rate-limits, and a 429 mid-run shouldn't lose the batch."""
    headers = {"User-Agent": UA}
    if key:
        headers["Authorization"] = "Bearer " + key
    for attempt in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45).read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def api(path, key):
    return json.loads(http(SGDB + path, key))


def first_600x900(payload):
    for g in (payload or {}).get("data") or []:
        if g.get("width") == 600 and g.get("height") == 900:
            return g.get("url")
    return None


def resolve(game, key):
    """game -> (sgdb_id, grid_url, how). Tries the display name, then HLTB's matchedName."""
    queries = [game.get("name")]
    if game.get("matchedName") and norm(game["matchedName"]) != norm(game.get("name")):
        queries.append(game["matchedName"])
    for q in queries:
        if not q:
            continue
        hits = api("/search/autocomplete/" + urllib.parse.quote(q), key).get("data") or []
        exact = [c for c in hits if norm(c.get("name")) == norm(q)]
        # First exact hit usually wins, but a few games have no 600x900 art on their entry.
        for cand in exact[:3]:
            url = first_600x900(api("/grids/game/%d?dimensions=600x900" % cand["id"], key))
            if url:
                return cand["id"], url, ("name" if q == game.get("name") else "matchedName")
    return None, None, None


def encode(raw, width, quality):
    """600x900 source -> 3:4 center crop -> width x (width*4/3) WebP."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    target_h = int(round(w * 4 / 3))
    if h > target_h:
        top = (h - target_h) // 2
        im = im.crop((0, top, w, top + target_h))
    im = im.resize((width, int(round(width * 4 / 3))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=quality, method=6)
    return buf.getvalue()


def pick(games, sample, limit):
    """Evenly spaced through the file, so a test run isn't all A-titles and repeats exactly."""
    idx = list(range(len(games)))
    if sample:
        step = max(1, len(games) // sample)
        idx = idx[::step][:sample]
    elif limit:
        idx = idx[:limit]
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, help="N games spread evenly through the list")
    ap.add_argument("--limit", type=int, help="first N games")
    ap.add_argument("--out", help="directory for the new .webp files (dry run)")
    ap.add_argument("--width", type=int, default=400)
    ap.add_argument("--quality", type=int, default=72)
    ap.add_argument("--ids-file", default=IDS_FILE)
    ap.add_argument("--apply", action="store_true", help="write covers back into master_games_final.json")
    ap.add_argument("--delay", type=float, default=0.25, help="seconds between SteamGridDB calls")
    args = ap.parse_args()

    if not args.apply and not args.out:
        sys.exit("give --out for a dry run, or --apply to update the master JSON")

    key = open(KEY_FILE, encoding="utf-8").read().strip()
    games = json.load(open(MASTER, encoding="utf-8"))
    ids = json.load(open(args.ids_file, encoding="utf-8")) if os.path.exists(args.ids_file) else {}

    if args.out:
        os.makedirs(args.out, exist_ok=True)

    targets = pick(games, args.sample, args.limit)
    report = []
    changed = 0
    print("%d games to refresh (%d in file)\n" % (len(targets), len(games)))

    for n, i in enumerate(targets, 1):
        g = games[i]
        name = g.get("name")
        row = {"index": i, "name": name, "old_bytes": None, "new_bytes": None, "status": "", "sgdb_id": None}
        old = g.get("cover") or ""
        if old.startswith("data:"):
            row["old_bytes"] = len(base64.b64decode(old.split(",", 1)[1]))
        try:
            cached = ids.get(name) or {}
            sgdb_id, url, how = cached.get("sgdbId"), cached.get("url"), "cache"
            if not url:
                sgdb_id, url, how = resolve(g, key)
                time.sleep(args.delay)
            if not url:
                row["status"] = "no exact match - kept old cover"
            else:
                data = encode(http(url), args.width, args.quality)
                row.update(new_bytes=len(data), sgdb_id=sgdb_id, status="ok (%s)" % how)
                ids[name] = {"sgdbId": sgdb_id, "url": url}
                if args.out:
                    with open(os.path.join(args.out, "%04d.webp" % i), "wb") as f:
                        f.write(data)
                if args.apply:
                    g["cover"] = "data:image/webp;base64," + base64.b64encode(data).decode()
                changed += 1
        except Exception as e:
            row["status"] = "ERROR " + str(e)[:120]
        report.append(row)
        print("%3d/%d  %-52.52s %s" % (n, len(targets), name, row["status"]))

    json.dump(ids, open(args.ids_file, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    if args.apply and changed:
        backup = MASTER.replace(".json", ".backup_precover_hires.json")
        if not os.path.exists(backup):
            shutil.copy(MASTER, backup)
            print("\nbacked up master -> " + os.path.basename(backup))
        json.dump(games, open(MASTER, "w", encoding="utf-8"), ensure_ascii=False)
        print("wrote %d covers into master_games_final.json (now run build.py)" % changed)

    if args.out:
        json.dump(report, open(os.path.join(args.out, "report.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    ok = [r for r in report if r["status"].startswith("ok")]
    old_kb = sum(r["old_bytes"] or 0 for r in ok) / 1024.0
    new_kb = sum(r["new_bytes"] or 0 for r in ok) / 1024.0
    print("\n%d/%d refreshed | %d no match | %d errors" % (
        len(ok), len(report),
        sum(1 for r in report if r["status"].startswith("no exact")),
        sum(1 for r in report if r["status"].startswith("ERROR"))))
    if ok and old_kb:
        print("avg %.0fKB -> %.0fKB per cover (%.1fx); all %d games would be about %.0fMB" % (
            old_kb / len(ok), new_kb / len(ok), new_kb / old_kb,
            len(games), new_kb / len(ok) * len(games) / 1024))


if __name__ == "__main__":
    main()
