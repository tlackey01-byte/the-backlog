"""Fold games added from the site into master_games_final.json.

Games added through the site's "+ Add Game" button live in Firestore
(users/{uid}/addedGames), not in the baked catalog. This script takes a site
"Export backup (.json)" file -- which includes those games alongside everything
else -- and appends any game whose name isn't already in the master file.

The Firestore copies don't need deleting afterwards: the games page skips any
added-game doc whose name already exists in the baked catalog.

Usage:
    python Source/bake_added_games.py path/to/backlog-backup-YYYY-MM-DD.json
    python Source/refetch_covers.py --apply --only Source/cover_review/baked_names.txt
    python Source/build.py
    python Source/upload_covers.py
    (then commit + push)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_PATH = os.path.join(HERE, "master_games_final.json")
IDS_PATH = os.path.join(HERE, "cover_sources.json")
NAMES_PATH = os.path.join(HERE, "cover_review", "baked_names.txt")


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
    if r.get("dv"):
        rec["developer"] = r["dv"]
    # Which release it is (see identify_games.py) -- the page and build.py's art check rely on it.
    if r.get("h") or r.get("hltbId"):
        rec["hltbId"] = r.get("h") or r.get("hltbId")
    if r.get("y"):
        rec["year"] = r["y"]
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
        json.dump(master, f, ensure_ascii=False)
    for g in new:
        print("Baked:", g["name"])

    # A site-added game has one portrait (made in the browser) and no hero or wide list image.
    # Pin the source it came from so refetch_covers.py can build the usual pair -- the same
    # image the site picked, now with SteamGridDB's capsule art (or the fallback) for the list.
    by_name = {r["n"]: r for r in backup["games"]}
    ids = json.load(open(IDS_PATH, encoding="utf-8")) if os.path.exists(IDS_PATH) else {}
    pinned = []
    for g in new:
        r = by_name[g["name"]]
        if r.get("cs") and g["name"] not in ids:
            ids[g["name"]] = {"sgdbId": r.get("sgdbId"), "url": r["cs"], "source": "site", "pinned": True}
            pinned.append(g["name"])
    if pinned:
        with open(IDS_PATH, "w", encoding="utf-8") as f:
            json.dump(ids, f, indent=1, ensure_ascii=False)
        os.makedirs(os.path.dirname(NAMES_PATH), exist_ok=True)
        with open(NAMES_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(pinned) + "\n")
    print(f"Added {len(new)} game(s).")
    if pinned:
        print("Next, build their cover pairs, then rebuild and upload:\n"
              f"  python Source/refetch_covers.py --apply --only {os.path.relpath(NAMES_PATH)}")
    print("Then: python Source/build.py, python Source/upload_covers.py, and commit + push.")


if __name__ == "__main__":
    main()
