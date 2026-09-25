"""
Rebuilds docs/games/index.html from master_games_final.json + games_template.html, and
keeps docs/sw.js's cache version in sync so the PWA service worker never serves a stale
build after a deploy.

Usage: python build.py
Run this from anywhere; paths below are absolute.

What it does:
1. Reads master_games_final.json (the source-of-truth dataset -- one object per game).
2. Compacts it into games_compact.json (short keys, matches what the template's JS expects).
   That file is only this build's scratch copy and is gitignored: the same data ships inside
   docs/games/index.html, so committing it too just doubled every data change in history.
3. Injects the compact JSON in place of games_template.html's __GAME_DATA_JSON__
   placeholder and writes the result to docs/games/index.html.
4. Hashes every deployed shell file (both pages + the shared CSS/JS) and rewrites
   docs/sw.js's CACHE_NAME to that hash -- the service worker caches pages cache-first,
   so without this step, anyone who already has it installed keeps seeing the old page
   forever after a deploy, no matter how the content changed (a game added or removed, a
   template/logic edit, a CSS tweak -- all of it changes this hash). Always run this
   through build.py rather than hand-editing CACHE_NAME; a manual bump is easy to forget
   and was exactly how a real page fix once shipped invisibly to already-visited devices.
5. Appends a fresh ?v=<timestamp> to the service worker registration URL in both pages.
   GitHub Pages serves everything, sw.js included, with Cache-Control: max-age=600 and
   there's no way to override that on GitHub Pages (no custom response headers) -- so for
   up to 10 minutes after a deploy, a browser's own HTTP cache (a separate, lower-level
   thing from the Cache Storage API CACHE_NAME above) can hand back stale bytes when it
   checks sw.js for updates, and neither a soft nor a hard refresh reliably works around
   that since it's the background update-check fetch that's affected, not the page
   navigation itself. A version query string makes that fetch a different URL every
   deploy, which the 600s cache can never have a hit for.

games_template.html *is* the deployed games page's structure and logic (Firebase login
gate, saveState()/onSnapshot wiring, the deletion banner, etc.) -- edit it, never
docs/games/index.html directly, since this script overwrites the latter on every run.

After running this: commit master_games_final.json, docs/games/index.html, docs/index.html
and docs/sw.js, then push -- GitHub Pages redeploys within about a minute. There's no
claude.ai artifact or vault copy to keep in sync anymore; the deployed site is the one true
copy.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_PATH = os.path.join(HERE, "master_games_final.json")
COMPACT_PATH = os.path.join(HERE, "games_compact.json")
TEMPLATE_PATH = os.path.join(HERE, "games_template.html")
DOCS_DIR = os.path.join(os.path.dirname(HERE), "docs")
GAMES_PAGE_OUT_PATH = os.path.join(DOCS_DIR, "games", "index.html")
HOMEPAGE_PATH = os.path.join(DOCS_DIR, "index.html")
BASE_CSS_PATH = os.path.join(DOCS_DIR, "shared", "base.css")
FIREBASE_INIT_PATH = os.path.join(DOCS_DIR, "shared", "firebase-init.js")
PULL_TO_REFRESH_PATH = os.path.join(DOCS_DIR, "shared", "pull-to-refresh.js")
NAV_PATH = os.path.join(DOCS_DIR, "shared", "nav.js")
ENV_PATH = os.path.join(DOCS_DIR, "shared", "env.js")
SW_PATH = os.path.join(DOCS_DIR, "sw.js")
COVERS_DIR = os.path.join(DOCS_DIR, "games", "covers")
HERO_DIR = os.path.join(COVERS_DIR, "hero")

# Where the page loads cover images from. The ~120MB of covers live in a Cloudflare R2 bucket
# served by the worker's /img/ route, not in this repo -- git would otherwise keep every
# version of every cover forever, and a re-bake at a different size could never be reclaimed
# without rewriting history. docs/games/covers/ is the local staging copy, gitignored, and
# Source/upload_covers.py pushes it to R2.
#
# Set BACKLOG_COVER_BASE=covers/ to build against those local files instead -- useful offline,
# and the way back if R2 ever needs to be abandoned.
COVER_BASE = os.environ.get("BACKLOG_COVER_BASE",
                            "https://backlog-proxy.tlackey01.workers.dev/img/")


def cover_name(data_uri):
    """(file name, decoded bytes) for a base64 cover -- the name write_cover() gives it.
    Separate so sync_site.py can tell that file is in use without writing anything."""
    header, b64 = data_uri.split(",", 1)
    raw = base64.b64decode(b64)
    # A .jpg file holding WebP bytes would be served as image/jpeg and may not render, so
    # take the type from the data URI rather than assuming.
    ext = "jpg"
    for mime, e in (("image/webp", "webp"), ("image/png", "png")):
        if mime in header:
            ext = e
            break
    return hashlib.sha1(raw).hexdigest()[:16] + "." + ext, raw


def write_cover(data_uri, written):
    """Writes one base64 cover out as docs/games/covers/<content hash>.webp and returns the
    file name the master file should store in place of the data URI.

    Covers normally arrive already on disk -- refetch_covers.py writes the file and records
    its name -- so this only runs for a cover that came in as base64, which today means a
    game added through the site before it uploaded covers, folded in by sync_site.py. Naming by content hash
    means an unchanged cover keeps its URL forever (caches stay valid across deploys) and a
    changed one gets a new URL automatically."""
    name, raw = cover_name(data_uri)
    if name not in written:
        path = os.path.join(COVERS_DIR, name)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(raw)
        written.add(name)
    return name


def build_compact():
    with open(MASTER_PATH, encoding="utf-8") as f:
        games = json.load(f)

    os.makedirs(COVERS_DIR, exist_ok=True)
    written_covers = set()
    written_heroes = set()
    compact = []
    for g in games:
        rec = {
            "n": g["name"],
            "m": g["main"],
            "e": g["extra"],
            "c": g["completionist"],
            "o": g["owned"],
            "p": g["platforms"],
            "g": g["genres"],
        }
        if g.get("progress"):
            rec["pr"] = g["progress"]
        if g.get("playedHours") is not None:
            rec["ph"] = g["playedHours"]
        # The master file stores file names ("<hash>.webp", "hero/<hash>.webp"); records
        # carry the full URL, so pointing covers at a CDN is a change to COVER_BASE alone.
        # A base64 cover is still accepted: an older site-added game keeps its embedded
        # portrait if refetch_covers.py finds nothing better when sync_site.py folds it in.
        cover = g.get("cover")
        if cover:
            name = write_cover(cover, written_covers) if cover.startswith("data:") else cover
            written_covers.add(name)
            rec["cv"] = COVER_BASE + name
        if g.get("coverHero"):
            written_heroes.add(os.path.basename(g["coverHero"]))
            rec["hv"] = COVER_BASE + g["coverHero"]
        if g.get("developer"):
            rec["dv"] = g["developer"]
        # Which release this is. The Add Game box matches on these, so searching "Doom" and
        # picking the 1993 game isn't mistaken for the 2016 one already in the list.
        if g.get("hltbId"):
            rec["h"] = g["hltbId"]
        if g.get("year"):
            rec["y"] = g["year"]
        compact.append(rec)

    with open(COMPACT_PATH, "w", encoding="utf-8") as f:
        json.dump(compact, f, ensure_ascii=False, separators=(",", ":"))

    # Drop cover files no game references anymore (a game deleted from the master file,
    # or its cover replaced -- names are content hashes, so a new image is a new file).
    stale = [n for n in os.listdir(COVERS_DIR)
             if n not in written_covers and os.path.isfile(os.path.join(COVERS_DIR, n))]
    for n in stale:
        os.remove(os.path.join(COVERS_DIR, n))

    # Same sweep for the hero files.
    stale_heroes = []
    if os.path.isdir(HERO_DIR):
        stale_heroes = [n for n in os.listdir(HERO_DIR) if n not in written_heroes]
        for n in stale_heroes:
            os.remove(os.path.join(HERO_DIR, n))
    print(f"Covers: {len(written_covers)} list ({len(stale)} stale removed), "
          f"{len(written_heroes)} heroes ({len(stale_heroes)} stale removed)")
    print(f"Cover URLs point at: {COVER_BASE or '(page-relative)'}")

    print(f"Compacted {len(compact)} games -> {COMPACT_PATH}")
    return compact


IDS_PATH = os.path.join(HERE, "cover_sources.json")


def check_art_years():
    """Warns about games whose cover art comes from a different release than the game.

    The first cover refresh matched art by name, and 30-odd games -- Doom, Prey, Resident
    Evil 2 and 4, Silent Hill 2 -- quietly showed the other release's art. Each game now
    records its own year (identify_games.py) and each cover the year
    of the SteamGridDB entry it came from (artYear), so a mismatch can be caught here, on
    every build, however the game was added. Warnings only: it never blocks a build.

    Silenced for a game when its art was picked by hand ("pinned", via the cover review) or
    was checked and is fine ("artYearOk": a PC port or re-release filed under a later year)."""
    if not os.path.exists(IDS_PATH):
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # game names aren't ASCII
    except Exception:
        pass
    with open(MASTER_PATH, encoding="utf-8") as f:
        games = json.load(f)
    with open(IDS_PATH, encoding="utf-8") as f:
        ids = json.load(f)
    off, unknown = [], 0
    for g in games:
        c = ids.get(g["name"]) or {}
        if not g.get("year"):
            unknown += 1
            continue
        if c.get("pinned") or c.get("artYearOk") or not c.get("artYear"):
            continue
        if abs(c["artYear"] - g["year"]) > 1:
            off.append("%s (%s, art from %s)" % (g["name"], g["year"], c["artYear"]))
    if off:
        print("WARNING: %d game(s) show art from a different release -- check them, then fix "
              "with the cover review or mark \"artYearOk\": true in cover_sources.json:" % len(off))
        for line in off:
            print("  - " + line)
    if unknown:
        print("Note: %d game(s) have no release year recorded -- run identify_games.py" % unknown)


def build_games_page(compact_json_str):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    page = template.replace("__GAME_DATA_JSON__", compact_json_str)
    with open(GAMES_PAGE_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote games page -> {GAMES_PAGE_OUT_PATH}")


# Both pages' registration: 'sw.js' on the homepage, '../sw.js' on the games page. Relative, so
# the same site works under GitHub Pages' /the-backlog/ and at a Cloudflare Pages preview's root.
SW_REGISTRATION_PATTERN = re.compile(r"register\('((?:\.\./)?sw\.js)(?:\?v=\d+)?'")


def bump_sw_registration_version():
    version = str(int(time.time()))
    for path in (TEMPLATE_PATH, HOMEPAGE_PATH):
        with open(path, encoding="utf-8") as f:
            content = f.read()
        new_content, count = SW_REGISTRATION_PATTERN.subn(
            lambda m: "register('%s?v=%s'" % (m.group(1), version), content)
        if count != 1:
            raise RuntimeError("Could not find service worker registration in " + path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    print(f"Bumped service worker registration cache-buster -> ?v={version}")
    return version


def update_service_worker_cache_name():
    shell_paths = [GAMES_PAGE_OUT_PATH, HOMEPAGE_PATH, BASE_CSS_PATH, FIREBASE_INIT_PATH, PULL_TO_REFRESH_PATH, NAV_PATH, ENV_PATH]
    h = hashlib.sha256()
    for p in shell_paths:
        with open(p, "rb") as f:
            h.update(f.read())
    new_hash = h.hexdigest()[:12]

    with open(SW_PATH, encoding="utf-8") as f:
        sw = f.read()
    new_sw, count = re.subn(
        r"const CACHE_NAME = '[^']*';",
        "const CACHE_NAME = 'the-backlog-shell-%s';" % new_hash,
        sw,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not find CACHE_NAME in " + SW_PATH)
    with open(SW_PATH, "w", encoding="utf-8") as f:
        f.write(new_sw)
    print(f"Updated service worker cache version -> the-backlog-shell-{new_hash}")


if __name__ == "__main__":
    bump_sw_registration_version()
    compact = build_compact()
    with open(COMPACT_PATH, encoding="utf-8") as f:
        compact_json_str = f.read()
    build_games_page(compact_json_str)
    update_service_worker_cache_name()
    check_art_years()
    print("Done. Commit + push to deploy via GitHub Pages.")
