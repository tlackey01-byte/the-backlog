"""
Rebuilds the standalone backlog.html from master_games_final.json + artifact_template.html.

Usage: python build.py
Run this from anywhere; paths below are absolute.

What it does:
1. Reads master_games_final.json (the source-of-truth dataset — one object per game).
2. Compacts it into games_compact.json (short keys, matches what the template's JS expects).
3. Wraps artifact_template.html's content into a full standalone HTML document
   (the template is an Artifact *fragment* — no <!doctype>/<html>/<head>/<body> —
   because the claude.ai artifact host adds that wrapper automatically; a local
   file needs it added manually or it renders in quirks mode).
4. Injects the compact JSON in place of the __GAME_DATA_JSON__ placeholder.
5. Writes the result to backlog.html (one level up, in Documents/Game Backlog/).

After running this, still need to:
- Push backlog.html to the vault copy (Projects/Game Backlog/Resources/backlog.html) via
  the Obsidian Local REST API (see the "Push to vault" section in the
  add-game-to-backlog skill for the curl command + API key).
- Republish the claude.ai artifact (Artifact tool, action publish, same url= as before)
  using the *unwrapped* fragment (build_fragment() below), not the standalone backlog.html.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_PATH = os.path.join(HERE, "master_games_final.json")
COMPACT_PATH = os.path.join(HERE, "games_compact.json")
TEMPLATE_PATH = os.path.join(HERE, "artifact_template.html")
FRAGMENT_OUT_PATH = os.path.join(HERE, "backlog_artifact_fragment.html")  # for claude.ai Artifact publish
STANDALONE_OUT_PATH = os.path.join(os.path.dirname(HERE), "backlog.html")  # for local file + vault


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


def build_fragment(compact_json_str):
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    fragment = template.replace("__GAME_DATA_JSON__", compact_json_str)
    with open(FRAGMENT_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(fragment)
    print(f"Wrote artifact fragment (for claude.ai publish) -> {FRAGMENT_OUT_PATH}")
    return fragment


def build_standalone(fragment):
    lines = fragment.split("\n", 1)
    title_line = lines[0]
    rest = lines[1] if len(lines) > 1 else ""
    wrapped = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"{title_line}\n</head>\n<body>\n{rest}\n</body>\n</html>\n"
    )
    with open(STANDALONE_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(wrapped)
    print(f"Wrote standalone HTML (for local file + vault) -> {STANDALONE_OUT_PATH}")


if __name__ == "__main__":
    compact = build_compact()
    with open(COMPACT_PATH, encoding="utf-8") as f:
        compact_json_str = f.read()
    fragment = build_fragment(compact_json_str)
    build_standalone(fragment)
    print("Done. Remember to push to vault + republish the claude.ai artifact.")
