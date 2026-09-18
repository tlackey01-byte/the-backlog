"""Fold games added from the site into master_games_final.json.

Games added through the site's "+ Add Game" button live in Firestore
(users/{uid}/addedGames), not in the baked catalog. This script takes a site
"Export backup (.json)" file -- which includes those games alongside everything
else -- and appends any game whose name isn't already in the master file.

The Firestore copies don't need deleting afterwards: the games page skips any
added-game doc whose name already exists in the baked catalog.

Usage:
    python Source/bake_added_games.py path/to/backlog-backup-YYYY-MM-DD.json
    python Source/build.py
    (then commit + push)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_PATH = os.path.join(HERE, "master_games_final.json")


def compact_to_master(r):
    """Inverse of build.py's build_compact() for a single record."""
    genres = r.get("g") or []
    # Backup exports fill an empty platform list with the 'Wishlist' stand-in; the
    # master file stores that case as [] (the page re-derives the default).
    platforms = [p for p in (r.get("p") or []) if p != "Wishlist"]
    rec = {
        "name": r["n"],
        "matchedName": r["n"],
        "main": r.get("m"),
        "extra": r.get("e"),
        "completionist": r.get("c"),
        "owned": bool(r.get("o")),
        "platforms": platforms,
        "genre": "; ".join(genres),
        "genres": genres,
    }
    if r.get("pr"):
        rec["progress"] = r["pr"]
    if r.get("ph") is not None:
        rec["playedHours"] = r["ph"]
    if r.get("cv"):
        rec["cover"] = r["cv"]
    return rec


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as f:
        backup = json.load(f)
    with open(MASTER_PATH, encoding="utf-8") as f:
        master = json.load(f)

    known = {g["name"] for g in master}
    new = [compact_to_master(r) for r in backup["games"] if r["n"] not in known]
    if not new:
        print("Nothing to bake -- every game in the backup is already in the master file.")
        return

    master.extend(new)
    with open(MASTER_PATH, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=1)  # matches the file's existing format
    for g in new:
        print("Baked:", g["name"])
    print(f"Added {len(new)} game(s). Now run build.py, then commit + push.")


if __name__ == "__main__":
    main()
