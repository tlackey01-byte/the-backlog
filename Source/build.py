"""
Rebuilds docs/games/index.html from master_games_final.json + games_template.html.

Usage: python build.py
Run this from anywhere; paths below are absolute.

What it does:
1. Reads master_games_final.json (the source-of-truth dataset -- one object per game).
2. Compacts it into games_compact.json (short keys, matches what the template's JS expects).
3. Injects the compact JSON in place of games_template.html's __GAME_DATA_JSON__
   placeholder and writes the result to docs/games/index.html.

games_template.html *is* the deployed games page's structure and logic (Firebase login
gate, saveState()/onSnapshot wiring, the deletion banner, etc.) -- edit it, never
docs/games/index.html directly, since this script overwrites the latter on every run.

After running this: commit master_games_final.json, games_compact.json, and
docs/games/index.html, then push -- GitHub Pages redeploys within about a minute. There's
no claude.ai artifact or vault copy to keep in sync anymore; the deployed site is the one
true copy.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_PATH = os.path.join(HERE, "master_games_final.json")
COMPACT_PATH = os.path.join(HERE, "games_compact.json")
TEMPLATE_PATH = os.path.join(HERE, "games_template.html")
GAMES_PAGE_OUT_PATH = os.path.join(os.path.dirname(HERE), "docs", "games", "index.html")


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


if __name__ == "__main__":
    compact = build_compact()
    with open(COMPACT_PATH, encoding="utf-8") as f:
        compact_json_str = f.read()
    build_games_page(compact_json_str)
    print("Done. Commit + push to deploy via GitHub Pages.")
