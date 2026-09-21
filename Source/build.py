"""
Rebuilds docs/games/index.html from master_games_final.json + games_template.html, and
keeps docs/sw.js's cache version in sync so the PWA service worker never serves a stale
build after a deploy.

Usage: python build.py
Run this from anywhere; paths below are absolute.

What it does:
1. Reads master_games_final.json (the source-of-truth dataset -- one object per game).
2. Compacts it into games_compact.json (short keys, matches what the template's JS expects).
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

After running this: commit master_games_final.json, games_compact.json, docs/games/index.html,
and docs/sw.js, then push -- GitHub Pages redeploys within about a minute. There's no
claude.ai artifact or vault copy to keep in sync anymore; the deployed site is the one true
copy.
"""
import base64
import hashlib
import json
import os
import re
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
SW_PATH = os.path.join(DOCS_DIR, "sw.js")
COVERS_DIR = os.path.join(DOCS_DIR, "games", "covers")
HERO_DIR = os.path.join(COVERS_DIR, "hero")
HEROES_PATH = os.path.join(HERE, "cover_heroes.json")


def write_cover(data_uri, written):
    """Writes one base64 cover out as docs/games/covers/<content hash>.jpg and returns the
    page-relative path the compact record points at instead of the inline data URI.

    Inlining ~1,950 covers made the games page ~13MB, which was slow to load and laggy on
    phones. As separate files they load lazily as rows scroll into view and get cached by
    the service worker (see sw.js's covers cache). Naming by content hash means an
    unchanged cover keeps its URL forever (cache stays valid across deploys), and a changed
    one gets a new URL automatically."""
    header, b64 = data_uri.split(",", 1)
    raw = base64.b64decode(b64)
    # Covers refreshed by refetch_covers.py are WebP; the original bake was JPEG, with a
    # stray PNG or two. Read the type off the data URI so a mixed master file bakes cleanly
    # -- a .jpg file holding WebP bytes would be served as image/jpeg and may not render.
    ext = "jpg"
    for mime, e in (("image/webp", "webp"), ("image/png", "png")):
        if mime in header:
            ext = e
            break
    name = hashlib.sha1(raw).hexdigest()[:16] + "." + ext
    if name not in written:
        path = os.path.join(COVERS_DIR, name)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(raw)
        written.add(name)
    return "covers/" + name


def build_compact():
    with open(MASTER_PATH, encoding="utf-8") as f:
        games = json.load(f)

    os.makedirs(COVERS_DIR, exist_ok=True)
    # Detail-page covers are full 600x900 files written straight into docs/ by
    # refetch_covers.py (too big to carry as base64 in the master file). This manifest maps
    # game name -> file, and a game without one just falls back to its thumbnail.
    heroes = {}
    if os.path.exists(HEROES_PATH):
        with open(HEROES_PATH, encoding="utf-8") as f:
            heroes = json.load(f)
    written_covers = set()
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
        if g.get("cover"):
            rec["cv"] = write_cover(g["cover"], written_covers)
        if heroes.get(g["name"]):
            # Manifest stores "hero/<hash>.webp"; records carry the page-relative path, the
            # same shape write_cover() returns for thumbnails.
            rec["hv"] = "covers/" + heroes[g["name"]]
        if g.get("developer"):
            rec["dv"] = g["developer"]
        compact.append(rec)

    with open(COMPACT_PATH, "w", encoding="utf-8") as f:
        json.dump(compact, f, ensure_ascii=False, separators=(",", ":"))

    # Drop cover files no game references anymore (a game deleted from the master file,
    # or its cover replaced -- names are content hashes, so a new image is a new file).
    stale = [n for n in os.listdir(COVERS_DIR)
             if n not in written_covers and os.path.isfile(os.path.join(COVERS_DIR, n))]
    for n in stale:
        os.remove(os.path.join(COVERS_DIR, n))

    # Same sweep for the hero files, against the manifest rather than the compact records.
    kept_heroes = {os.path.basename(v) for v in heroes.values()}
    stale_heroes = []
    if os.path.isdir(HERO_DIR):
        stale_heroes = [n for n in os.listdir(HERO_DIR) if n not in kept_heroes]
        for n in stale_heroes:
            os.remove(os.path.join(HERO_DIR, n))
    print(f"Covers: {len(written_covers)} thumbs ({len(stale)} stale removed), "
          f"{len(kept_heroes)} heroes ({len(stale_heroes)} stale removed)")

    print(f"Compacted {len(compact)} games -> {COMPACT_PATH}")
    return compact


def build_games_page(compact_json_str):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    page = template.replace("__GAME_DATA_JSON__", compact_json_str)
    with open(GAMES_PAGE_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote games page -> {GAMES_PAGE_OUT_PATH}")


SW_REGISTRATION_PATTERN = re.compile(r"register\('/the-backlog/sw\.js(?:\?v=\d+)?'")


def bump_sw_registration_version():
    version = str(int(time.time()))
    replacement = "register('/the-backlog/sw.js?v=%s'" % version
    for path in (TEMPLATE_PATH, HOMEPAGE_PATH):
        with open(path, encoding="utf-8") as f:
            content = f.read()
        new_content, count = SW_REGISTRATION_PATTERN.subn(replacement, content)
        if count != 1:
            raise RuntimeError("Could not find service worker registration in " + path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    print(f"Bumped service worker registration cache-buster -> ?v={version}")
    return version


def update_service_worker_cache_name():
    shell_paths = [GAMES_PAGE_OUT_PATH, HOMEPAGE_PATH, BASE_CSS_PATH, FIREBASE_INIT_PATH, PULL_TO_REFRESH_PATH, NAV_PATH]
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
    print("Done. Commit + push to deploy via GitHub Pages.")
