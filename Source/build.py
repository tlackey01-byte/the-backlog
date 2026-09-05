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

games_template.html *is* the deployed games page's structure and logic (Firebase login
gate, saveState()/onSnapshot wiring, the deletion banner, etc.) -- edit it, never
docs/games/index.html directly, since this script overwrites the latter on every run.

After running this: commit master_games_final.json, games_compact.json, docs/games/index.html,
and docs/sw.js, then push -- GitHub Pages redeploys within about a minute. There's no
claude.ai artifact or vault copy to keep in sync anymore; the deployed site is the one true
copy.
"""
import hashlib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_PATH = os.path.join(HERE, "master_games_final.json")
COMPACT_PATH = os.path.join(HERE, "games_compact.json")
TEMPLATE_PATH = os.path.join(HERE, "games_template.html")
DOCS_DIR = os.path.join(os.path.dirname(HERE), "docs")
GAMES_PAGE_OUT_PATH = os.path.join(DOCS_DIR, "games", "index.html")
HOMEPAGE_PATH = os.path.join(DOCS_DIR, "index.html")
BASE_CSS_PATH = os.path.join(DOCS_DIR, "shared", "base.css")
FIREBASE_INIT_PATH = os.path.join(DOCS_DIR, "shared", "firebase-init.js")
SW_PATH = os.path.join(DOCS_DIR, "sw.js")


def build_compact():
    with open(MASTER_PATH, encoding="utf-8") as f:
        games = json.load(f)

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
            rec["cv"] = g["cover"]
        compact.append(rec)

    with open(COMPACT_PATH, "w", encoding="utf-8") as f:
        json.dump(compact, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Compacted {len(compact)} games -> {COMPACT_PATH}")
    return compact


def build_games_page(compact_json_str):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    page = template.replace("__GAME_DATA_JSON__", compact_json_str)
    with open(GAMES_PAGE_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote games page -> {GAMES_PAGE_OUT_PATH}")


def update_service_worker_cache_name():
    shell_paths = [GAMES_PAGE_OUT_PATH, HOMEPAGE_PATH, BASE_CSS_PATH, FIREBASE_INIT_PATH]
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
    compact = build_compact()
    with open(COMPACT_PATH, encoding="utf-8") as f:
        compact_json_str = f.read()
    build_games_page(compact_json_str)
    update_service_worker_cache_name()
    print("Done. Commit + push to deploy via GitHub Pages.")
