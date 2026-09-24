"""Records which release each game in the master file is: its HowLongToBeat id and year.

A name alone doesn't say which game an entry is -- "Doom" is a 1993 game and a 2016 one,
"Dead Space" a 2008 one and a 2023 remake -- and without that, nothing downstream can tell
whether an entry's hours or cover art belong to it. The first cover refresh matched art by
name only, and dozens of games ended up with the other release's art.

How each entry is identified, most certain first:
  - a "(year)" in matchedName ("Doom (2016)") picks that release;
  - otherwise the entry's hours, which were copied from one HLTB release, pick the closest;
  - one release by that name on HLTB is simply that one.
It also records the release year of the SteamGridDB entry each game's art came from
(`artYear` in cover_sources.json), which build.py compares against the game's own year.

Results go to cover_review/identity.json as they come in (resumable), and are written into
the master file (`hltbId`, `year`) and cover_sources.json (`artYear`) only at the end.

  python Source/identify_games.py              # games still missing an hltbId
  python Source/identify_games.py --report     # list names that have more than one release
  python Source/identify_games.py --developers # fill `developer` from each game's HLTB page

--developers needs the hltbId from the first form: HLTB's search results don't carry the
developer, only each game's own page does, so it's one page fetch per game -- by id, so
there's nothing to match or guess. Only games with no developer yet are fetched.
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from refetch_covers import HERE, IDS_FILE, KEY_FILE, MASTER, api
from find_cover_candidates import REVIEW_DIR, YEAR_SUFFIX, Hltb, fold, hours_gap, year_of

OUT = os.path.join(REVIEW_DIR, "identity.json")
_local = threading.local()


def hltb():
    if not hasattr(_local, "h"):
        _local.h = Hltb()
    return _local.h


def identify(g):
    """{hltbId, year, hltbName, how, versions} for one master entry."""
    q = g.get("matchedName") or g["name"]
    m = YEAR_SUFFIX.match(q)
    base, want = (m.group(1), int(m.group(2))) if m else (q, None)
    same = []
    for query in dict.fromkeys([base, YEAR_SUFFIX.sub(r"\1", g["name"])]):
        hits = hltb().search(query)
        time.sleep(0.3)
        same = [h for h in hits if fold(h.get("game_name")) == fold(query)]
        if same:
            break
    yr = lambda h: int(h.get("release_world") or 0) or None
    versions = [{"hltbId": h["game_id"], "name": h.get("game_name"), "year": yr(h),
                 "platforms": h.get("profile_platform") or "",
                 "hours": [round(h[k] / 1800) / 2 if h.get(k) else None for k in ("comp_main", "comp_plus", "comp_100")]}
                for h in same]
    row = {"versions": versions}
    if not same:
        return dict(row, how="no HLTB match")
    if want:
        near = sorted((h for h in same if yr(h) and abs(yr(h) - want) <= 1), key=lambda h: abs(yr(h) - want))
        pick, how = (near[0], "year in matchedName") if near else (None, "no release from %d" % want)
    elif len(same) == 1:
        pick, how = same[0], "only release"
    else:
        ranked = sorted(same, key=lambda h: hours_gap(g, h))
        gap = hours_gap(g, ranked[0])
        # Hours that sit clearly closer to one release settle it; a tie (or no hours) doesn't.
        if gap == float("inf") or (hours_gap(g, ranked[1]) - gap) < 0.25:
            pick, how = None, "ambiguous: %d releases, hours don't decide" % len(same)
        else:
            pick, how = ranked[0], "closest hours"
    if pick:
        row.update(hltbId=pick["game_id"], year=yr(pick), hltbName=pick.get("game_name"))
    return dict(row, how=how)


def art_year(sgdb_id, key):
    try:
        return year_of((api("/games/id/%d" % sgdb_id, key).get("data") or {}).get("release_date"))
    except Exception:
        return None


def fill_developers(workers):
    """`developer` for every identified game that lacks one, from its HLTB page."""
    games = json.load(open(MASTER, encoding="utf-8"))
    todo = [g for g in games if g.get("hltbId") and not g.get("developer")]
    print("%d games to look up" % len(todo), flush=True)

    def work(g):
        try:
            page = hltb().page(int(g["hltbId"])) or {}
            time.sleep(0.3)
        except Exception:
            return g["name"], None
        # A comma list for co-developed games ("Remedy Entertainment, Nitro Games"); keep it
        # as-is -- the page shows it verbatim, as it does for games added from the site.
        return g["name"], (page.get("profile_dev") or "").strip() or None

    found = {}
    with ThreadPoolExecutor(workers) as ex:
        for n, (name, dev) in enumerate(ex.map(work, todo), 1):
            if dev:
                found[name] = dev
            if n % 100 == 0 or n == len(todo):
                print("%d/%d (%d found)" % (n, len(todo), len(found)), flush=True)
    games = json.load(open(MASTER, encoding="utf-8"))   # re-read: write into the current file
    for g in games:
        if g["name"] in found and not g.get("developer"):
            g["developer"] = found[g["name"]]
    json.dump(games, open(MASTER, "w", encoding="utf-8"), ensure_ascii=False)
    print("developer set on %d games; %d have one now, %d don't" % (
        len(found), sum(1 for g in games if g.get("developer")), sum(1 for g in games if not g.get("developer"))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="print names with more than one release and stop")
    ap.add_argument("--developers", action="store_true", help="fill `developer` from each identified game's HLTB page")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.developers:
        return fill_developers(args.workers)

    games = json.load(open(MASTER, encoding="utf-8"))
    ids = json.load(open(IDS_FILE, encoding="utf-8"))
    done = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}

    if args.report:
        for g in games:
            r = done.get(g["name"]) or {}
            if len({v["year"] for v in r.get("versions") or []}) > 1:
                print("%-50s is %-6s  releases: %s" % (g["name"][:50], r.get("year") or "?",
                      ", ".join(str(v["year"]) for v in r["versions"])))
        return

    key = open(KEY_FILE, encoding="utf-8").read().strip()
    todo = [g for g in games if not g.get("hltbId") and g["name"] not in done]
    print("%d games to identify (%d already in %s)" % (len(todo), len(done), os.path.basename(OUT)), flush=True)
    lock = threading.Lock()

    def work(g):
        try:
            row = identify(g)
        except Exception as e:
            return g["name"], {"how": "ERROR " + str(e)[:120]}
        c = ids.get(g["name"]) or {}
        if c.get("sgdbId") and "artYear" not in c:
            row["artYear"] = art_year(c["sgdbId"], key)
            time.sleep(0.2)
        return g["name"], row

    errors = []
    with ThreadPoolExecutor(args.workers) as ex:
        for n, (name, row) in enumerate(ex.map(work, todo), 1):
            with lock:
                # A failed lookup isn't saved, so the next run retries it -- but say so.
                if row["how"].startswith("ERROR"):
                    errors.append(name)
                else:
                    done[name] = row
                if n % 50 == 0 or n == len(todo):
                    json.dump(done, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
                    print("%d/%d" % (n, len(todo)), flush=True)

    # Write into the master file and cache in one go, now that everything is in.
    for g in games:
        r = done.get(g["name"]) or {}
        if r.get("hltbId") and not g.get("hltbId"):
            g["hltbId"], g["year"] = r["hltbId"], r.get("year")
        c = ids.get(g["name"])
        if c is not None and r.get("artYear") and "artYear" not in c:
            c["artYear"] = r["artYear"]
    json.dump(games, open(MASTER, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(ids, open(IDS_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    hows = {}
    for g in games:
        h = (done.get(g["name"]) or {}).get("how") or ("had one" if g.get("hltbId") else "not run")
        h = h.split(":")[0] if h.startswith("ambiguous") else h
        hows[h] = hows.get(h, 0) + 1
    print("identified %d of %d games" % (sum(1 for g in games if g.get("hltbId")), len(games)))
    if errors:
        print("%d lookups failed (rate limits?) -- run again to retry just those" % len(errors))
    for h, n in sorted(hows.items(), key=lambda x: -x[1]):
        print("  %-28s %d" % (h, n))


if __name__ == "__main__":
    main()
