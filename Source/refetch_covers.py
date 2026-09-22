"""Re-fetch game covers from SteamGridDB at full 600x900 and re-bake them in two sizes.

Why two: the list shows ~1,950 covers at 64x96 (phone) or 88x132 (table), while the detail
page shows one at 200x300 (phone) or 300x450 (desktop). One file can't serve both without
waste -- a hero-sized image is ~55KB, and scrolling the whole list through those would be
~105MB of phone data for thumbnails nobody looks at closely. So:

  wide   460x215  ~14KB   docs/games/covers/<hash>.webp, used by rows and the table
  hero   600x900  ~55KB   docs/games/covers/hero/<hash>.webp, detail page only

The list image is Steam capsule art, not the portrait grid: rows show it at 152x71, and
squeezing 2:3 art into that box crops away the top and bottom, which is where box art puts
its logo. About 7% of games have no capsule art on SteamGridDB (retro and console-only
titles mostly); those fall back to the portrait cover centered on a blurred blow-up of
itself, which reads as deliberate rather than as a crop gone wrong.

The hero is SteamGridDB's native size untouched, which is exactly what a 300x450 box needs
on a 2x display and a 200x300 box needs on a phone at 3x.

Both are written to disk as content-hashed files; the master file records only their names
(`cover` and `coverHero`). Carrying the bytes as base64 in that file made it 37MB, and since
base64 doesn't delta-compress, every commit wrote a near-complete fresh copy.

Matching mirrors worker/src/index.js `details()`: SteamGridDB autocomplete, keep only
candidates whose normalized name equals the game's. A game with no exact match keeps its
existing cover and is listed in the report for review -- guessing silently would swap in art
for the wrong game. Resolved ids are cached to --ids-file so later runs skip the search.

  # dry run: 30 evenly spaced games, both images + a report into a preview dir
  python Source/refetch_covers.py --sample 30 --out <dir>

  # full run: both images into docs/games/covers/, names into the master file (backed up first)
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
COVERS_DIR = os.path.join(DOCS, "games", "covers")
HERO_DIR = os.path.join(COVERS_DIR, "hero")
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


def widest(payload):
    """Biggest landscape capsule available -- 920x430 when there is one, else 460x215."""
    grids = sorted((payload or {}).get("data") or [], key=lambda g: -(g.get("width") or 0))
    return grids[0].get("url") if grids else None


def make_wide(portrait, capsule, width, quality):
    """Capsule art resized to width x (width*215/460), or a fallback built from the portrait.

    The fallback blurs and darkens a blown-up slice of the cover to fill the wide box, then
    drops the sharp cover on top at full height -- nothing is cropped away, and it doesn't
    read as a mistake next to the real capsules."""
    from PIL import Image, ImageFilter
    height = int(round(width * 215.0 / 460.0))
    if capsule is not None:
        out = capsule.resize((width, height), Image.LANCZOS)
    else:
        tall = portrait.resize((width, int(round(width * 1.5))), Image.LANCZOS)
        top = (tall.height - height) // 2
        out = tall.crop((0, top, width, top + height)).filter(ImageFilter.GaussianBlur(width // 26))
        out = out.point(lambda px: int(px * 0.55))
        fg_w = int(round(height * 2 / 3.0))
        out.paste(portrait.resize((fg_w, height), Image.LANCZOS), ((width - fg_w) // 2, 0))
    buf = io.BytesIO()
    out.save(buf, "WEBP", quality=quality, method=6)
    return buf.getvalue()


def resolve(game, key):
    """game -> (sgdb_id, grid_url, how). Tries the display name, then HLTB's matchedName."""
    queries = [game.get("name")]
    if game.get("matchedName") and norm(game["matchedName"]) != norm(game.get("name")):
        queries.append(game["matchedName"])
    for q in queries:
        if not q:
            continue
        # safe="" matters: quote() leaves "/" alone by default, so a name like "Split/Second"
        # turns into an extra path segment and the API 404s.
        hits = api("/search/autocomplete/" + urllib.parse.quote(q, safe=""), key).get("data") or []
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
    ap.add_argument("--wide-width", type=int, default=460)
    ap.add_argument("--hero-width", type=int, default=600)
    ap.add_argument("--wide-quality", type=int, default=72)
    ap.add_argument("--hero-quality", type=int, default=65)
    ap.add_argument("--ids-file", default=IDS_FILE)
    ap.add_argument("--apply", action="store_true", help="write cover files into docs/ and record their names in the master JSON")
    ap.add_argument("--force", action="store_true", help="redo games that already have refreshed art")
    ap.add_argument("--delay", type=float, default=0.25, help="seconds between SteamGridDB calls")
    args = ap.parse_args()

    # Windows consoles default to cp1252, which can't print half the library -- Pokemon
    # Omega Ruby is fine, Shin Megami Tensei's "Ō" is not, and a crash there would throw
    # away an hour of downloads over a progress line.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not args.apply and not args.out:
        sys.exit("give --out for a dry run, or --apply to update the master JSON and docs/")

    from PIL import Image

    key = open(KEY_FILE, encoding="utf-8").read().strip()
    games = json.load(open(MASTER, encoding="utf-8"))
    ids = json.load(open(args.ids_file, encoding="utf-8")) if os.path.exists(args.ids_file) else {}

    if args.out:
        os.makedirs(os.path.join(args.out, "wide"), exist_ok=True)
        os.makedirs(os.path.join(args.out, "hero"), exist_ok=True)
    if args.apply:
        os.makedirs(HERO_DIR, exist_ok=True)

    targets = pick(games, args.sample, args.limit)
    report = []
    changed = 0
    print("%d games to refresh (%d in file)\n" % (len(targets), len(games)))

    if args.apply:
        # Back up before touching anything rather than after: a full run takes a couple of
        # hours, and surviving that going wrong is the whole point of the backup. Written
        # once only, so it keeps holding the original covers however often this is re-run.
        backup = MASTER.replace(".json", ".backup_precover_hires.json")
        if not os.path.exists(backup):
            shutil.copy(MASTER, backup)
            print("backed up master -> %s\n" % os.path.basename(backup))

    def checkpoint():
        """Flush progress to disk. A ~1,950-game run is long enough that losing it all to one
        bad request at game 1,800 would be miserable; hero files land as they go, so the
        master file and both manifests have to keep pace with them."""
        json.dump(ids, open(args.ids_file, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        if args.apply:
            json.dump(games, open(MASTER, "w", encoding="utf-8"), ensure_ascii=False)

    for n, i in enumerate(targets, 1):
        g = games[i]
        name = g.get("name")
        row = {"index": i, "name": name, "old_bytes": None, "wide_bytes": None,
               "hero_bytes": None, "status": "", "sgdb_id": None, "wide_src": None}
        old = g.get("cover") or ""
        old_path = os.path.join(COVERS_DIR, old) if old and not old.startswith("data:") else None
        if old_path and os.path.exists(old_path):
            row["old_bytes"] = os.path.getsize(old_path)
        # Resume cheaply: a game that already has both a WebP list image and a hero file was
        # done on an earlier run, and redoing it would re-download ~1MB to produce the same
        # bytes. --force redoes them anyway, e.g. after changing a size or quality setting.
        if args.apply and not args.force and old.endswith(".webp") and g.get("coverHero"):
            row["status"] = "already refreshed - skipped"
            report.append(row)
            print("%3d/%d  %-52.52s %s" % (n, len(targets), name, row["status"]), flush=True)
            continue
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
                hero = encode(src, args.hero_width, args.hero_quality)
                hero_name = "hero/" + hashlib.sha1(hero).hexdigest()[:16] + ".webp"
                # Landscape capsule for the list. Cached alongside the portrait url so a
                # re-run doesn't pay for the lookup twice.
                cap_url = cached.get("wideUrl")
                if not cap_url and "wideUrl" not in cached:
                    cap_url = widest(api("/grids/game/%d?dimensions=460x215,920x430" % sgdb_id, key))
                    time.sleep(args.delay)
                capsule = Image.open(io.BytesIO(http(cap_url))).convert("RGB") if cap_url else None
                wide = make_wide(src, capsule, args.wide_width, args.wide_quality)
                row.update(wide_bytes=len(wide), hero_bytes=len(hero), sgdb_id=sgdb_id,
                           wide_src="capsule" if capsule else "fallback",
                           status="ok (%s, %s)" % (how, "capsule" if capsule else "no capsule"))
                ids[name] = {"sgdbId": sgdb_id, "url": url, "wideUrl": cap_url}
                if args.out:
                    open(os.path.join(args.out, "wide", "%04d.webp" % i), "wb").write(wide)
                    open(os.path.join(args.out, "hero", "%04d.webp" % i), "wb").write(hero)
                if args.apply:
                    # Both images go to disk as content-hashed files and the master file
                    # records only their names. Carrying them as base64 instead made that
                    # file 37MB, and every commit wrote a fresh un-deltaable copy of it.
                    wide_name = hashlib.sha1(wide).hexdigest()[:16] + ".webp"
                    for rel, data in ((wide_name, wide), (hero_name, hero)):
                        path = os.path.join(COVERS_DIR, rel)
                        if not os.path.exists(path):
                            open(path, "wb").write(data)
                    g["cover"] = wide_name
                    g["coverHero"] = hero_name
                changed += 1
        except Exception as e:
            row["status"] = "ERROR " + str(e)[:120]
        report.append(row)
        print("%3d/%d  %-52.52s %s" % (n, len(targets), name, row["status"]), flush=True)
        if n % 50 == 0:
            checkpoint()

    checkpoint()

    if args.apply and changed:
        print("\nwrote %d list images and %d heroes into docs/games/covers/ (names recorded in the master file)"
              % (changed, changed))
        print("now run build.py")

    if args.out:
        json.dump(report, open(os.path.join(args.out, "report.json"), "w", encoding="utf-8"),
                  indent=1, ensure_ascii=False)

    ok = [r for r in report if r["status"].startswith("ok")]
    print("\n%d/%d refreshed | %d already done | %d no match | %d errors" % (
        len(ok), len(report),
        sum(1 for r in report if r["status"].startswith("already")),
        sum(1 for r in report if r["status"].startswith("no exact")),
        sum(1 for r in report if r["status"].startswith("ERROR"))))
    if ok:
        t = sum(r["wide_bytes"] for r in ok) / len(ok) / 1024.0
        h = sum(r["hero_bytes"] for r in ok) / len(ok) / 1024.0
        fb = sum(1 for r in ok if r["wide_src"] == "fallback")
        print("list avg %.0fKB (%d games = %.0fMB) | hero avg %.0fKB (%.0fMB)" % (
            t, len(games), t * len(games) / 1024, h, h * len(games) / 1024))
        print("%d of %d used real capsule art, %d fell back to the portrait treatment" % (
            len(ok) - fb, len(ok), fb))


if __name__ == "__main__":
    main()
