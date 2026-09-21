"""Re-fetch game covers from SteamGridDB at full 600x900 and re-bake them in two sizes.

Why two: the list shows ~1,950 covers at 64x96 (phone) or 88x132 (table), while the detail
page shows one at 200x300 (phone) or 300x450 (desktop). One file can't serve both without
waste -- a hero-sized image is ~55KB, and scrolling the whole list through those would be
~105MB of phone data for thumbnails nobody looks at closely. So:

  thumb  240x360  ~14KB   baked into master_games_final.json, used by rows and the table
  hero   600x900  ~55KB   written straight to docs/games/covers/hero/, detail page only

The hero is SteamGridDB's native size untouched, which is exactly what a 300x450 box needs
on a 2x display and a 200x300 box needs on a phone at 3x. Heroes are listed in
Source/cover_heroes.json (game name -> file) so build.py can emit them and clean up stale
ones; keeping them out of the master JSON keeps that file small enough to commit.

Both sizes are 2:3, the shape the art actually is -- the old bake cropped to 3:4 to match
the boxes, which is now handled by object-fit: cover on the smaller boxes instead.

Matching mirrors worker/src/index.js `details()`: SteamGridDB autocomplete, keep only
candidates whose normalized name equals the game's. A game with no exact match keeps its
existing cover and is listed in the report for review -- guessing silently would swap in art
for the wrong game. Resolved ids are cached to --ids-file so later runs skip the search.

  # dry run: 30 evenly spaced games, both sizes + a report into a preview dir
  python Source/refetch_covers.py --sample 30 --out <dir>

  # full run: thumbs into the master JSON (backed up first), heroes into docs/
  python Source/refetch_covers.py --apply

Needs: Pillow (WebP) and Source/steamgriddb_api_key.txt.
"""

import argparse
import base64
import hashlib
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
DOCS = os.path.join(os.path.dirname(HERE), "docs")
MASTER = os.path.join(HERE, "master_games_final.json")
KEY_FILE = os.path.join(HERE, "steamgriddb_api_key.txt")
IDS_FILE = os.path.join(HERE, "cover_sources.json")
HEROES_FILE = os.path.join(HERE, "cover_heroes.json")
HERO_DIR = os.path.join(DOCS, "games", "covers", "hero")
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


def encode(im, width, quality):
    """2:3 at the given width. No crop -- the art is already 2:3."""
    from PIL import Image
    out = im if im.width == width else im.resize((width, int(round(width * 1.5))), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "WEBP", quality=quality, method=6)
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
    ap.add_argument("--out", help="directory for the new files (dry run)")
    ap.add_argument("--thumb-width", type=int, default=240)
    ap.add_argument("--hero-width", type=int, default=600)
    ap.add_argument("--thumb-quality", type=int, default=72)
    ap.add_argument("--hero-quality", type=int, default=65)
    ap.add_argument("--ids-file", default=IDS_FILE)
    ap.add_argument("--apply", action="store_true", help="write thumbs into the master JSON and heroes into docs/")
    ap.add_argument("--delay", type=float, default=0.25, help="seconds between SteamGridDB calls")
    args = ap.parse_args()

    if not args.apply and not args.out:
        sys.exit("give --out for a dry run, or --apply to update the master JSON and docs/")

    from PIL import Image

    key = open(KEY_FILE, encoding="utf-8").read().strip()
    games = json.load(open(MASTER, encoding="utf-8"))
    ids = json.load(open(args.ids_file, encoding="utf-8")) if os.path.exists(args.ids_file) else {}
    heroes = json.load(open(HEROES_FILE, encoding="utf-8")) if os.path.exists(HEROES_FILE) else {}

    if args.out:
        os.makedirs(os.path.join(args.out, "thumb"), exist_ok=True)
        os.makedirs(os.path.join(args.out, "hero"), exist_ok=True)
    if args.apply:
        os.makedirs(HERO_DIR, exist_ok=True)

    targets = pick(games, args.sample, args.limit)
    report = []
    changed = 0
    print("%d games to refresh (%d in file)\n" % (len(targets), len(games)))

    for n, i in enumerate(targets, 1):
        g = games[i]
        name = g.get("name")
        row = {"index": i, "name": name, "old_bytes": None, "thumb_bytes": None,
               "hero_bytes": None, "status": "", "sgdb_id": None}
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
                src = Image.open(io.BytesIO(http(url))).convert("RGB")
                thumb = encode(src, args.thumb_width, args.thumb_quality)
                hero = encode(src, args.hero_width, args.hero_quality)
                hero_name = "hero/" + hashlib.sha1(hero).hexdigest()[:16] + ".webp"
                row.update(thumb_bytes=len(thumb), hero_bytes=len(hero), sgdb_id=sgdb_id,
                           status="ok (%s)" % how)
                ids[name] = {"sgdbId": sgdb_id, "url": url}
                if args.out:
                    open(os.path.join(args.out, "thumb", "%04d.webp" % i), "wb").write(thumb)
                    open(os.path.join(args.out, "hero", "%04d.webp" % i), "wb").write(hero)
                if args.apply:
                    g["cover"] = "data:image/webp;base64," + base64.b64encode(thumb).decode()
                    path = os.path.join(DOCS, "games", "covers", hero_name)
                    if not os.path.exists(path):
                        open(path, "wb").write(hero)
                    heroes[name] = hero_name
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
        json.dump(heroes, open(HEROES_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print("wrote %d thumbs into master_games_final.json and %d heroes into docs/games/covers/hero/"
              % (changed, changed))
        print("now run build.py")

    if args.out:
        json.dump(report, open(os.path.join(args.out, "report.json"), "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)

    ok = [r for r in report if r["status"].startswith("ok")]
    print("\n%d/%d refreshed | %d no match | %d errors" % (
        len(ok), len(report),
        sum(1 for r in report if r["status"].startswith("no exact")),
        sum(1 for r in report if r["status"].startswith("ERROR"))))
    if ok:
        t = sum(r["thumb_bytes"] for r in ok) / len(ok) / 1024.0
        h = sum(r["hero_bytes"] for r in ok) / len(ok) / 1024.0
        print("thumb avg %.0fKB (%d games = %.0fMB) | hero avg %.0fKB (%.0fMB)" % (
            t, len(games), t * len(games) / 1024, h, h * len(games) / 1024))


if __name__ == "__main__":
    main()
