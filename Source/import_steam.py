"""Brings your Steam library into the catalog: which games you own there, and how long you've played.

Reads your owned games and their playtime from the Steam Web API and updates the catalog's
built-in defaults in master_games_final.json. It never writes Firestore, so anything you've set
on the site still wins: a platform or hours value set there overrides these defaults.

  - A backlog game you own on Steam gets Steam as a platform, and so counts as owned.
  - Its hours played become Steam's playtime when that's more than the catalog has. As on the
    site, hours show for a game whose status isn't Backlog; a Backlog game reads 0h.

It changes nothing else and adds no games. The report also lists Steam games that aren't in
the backlog (add them with the site's + button), backlog games marked Steam that your library
doesn't have, and games you've played that are still in Backlog -- a quick way to put statuses
back.

Matching is by name (accents, numerals, editions and "The" evened out). When the backlog holds
more than one release of a title, the Steam app's release year picks between them. Anything
uncertain is listed rather than applied; settle it in Source/steam_matches.json, which maps a
Steam app id to a backlog name, or to null for "not in the backlog".

Needs Source/steam_api.json (gitignored): {"key": "<Steam Web API key>", "steamid": "<SteamID64>"}
  key       https://steamcommunity.com/dev/apikey (any domain name will do, e.g. localhost)
  steamid   the 17-digit number in your profile's URL, or look it up at steamid.io
and your Steam profile's "Game details" set to Public (Edit Profile -> Privacy Settings).

  python Source/import_steam.py           # show what it would change, change nothing
  python Source/import_steam.py --apply   # update the master file, then run build.py
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(HERE, "master_games_final.json")
CREDS = os.path.join(HERE, "steam_api.json")
MATCHES = os.path.join(HERE, "steam_matches.json")
KEY_FILE = os.path.join(HERE, "firebase_service_account.json")
OWNED_API = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
APP_API = "https://store.steampowered.com/api/appdetails?appids=%d&filters=release_date"

sys.path.insert(0, HERE)
from find_cover_candidates import core, match_level  # noqa: E402  (the catalog's forgiving name matcher)

# Only these levels are applied without a look; "loose" ones are listed for review.
SURE = ("exact", "folded", "core")


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "the-backlog-import"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def steam_library():
    """[{appid, name, minutes}] from the Steam Web API."""
    if not os.path.exists(CREDS):
        sys.exit("missing %s -- see this file's docstring" % CREDS)
    creds = json.load(open(CREDS, encoding="utf-8"))
    if "PASTE" in creds.get("key", "") + creds.get("steamid", ""):
        sys.exit("fill in your key and steamid in %s first" % CREDS)
    q = urllib.parse.urlencode({"key": creds["key"], "steamid": creds["steamid"], "include_appinfo": 1,
                                "include_played_free_games": 1, "format": "json"})
    try:
        games = get_json(OWNED_API + "?" + q).get("response", {}).get("games")
    except urllib.error.HTTPError as e:
        sys.exit("Steam refused the request (%d): check the key and steamid in %s" % (e.code, CREDS))
    if not games:
        sys.exit("Steam returned no games: set your profile's Game details to Public "
                 "(Edit Profile -> Privacy Settings) and try again")
    return [{"appid": g["appid"], "name": g.get("name") or str(g["appid"]),
             "minutes": g.get("playtime_forever") or 0} for g in games]


def release_year(appid):
    """The Steam app's release year, or None."""
    try:
        data = get_json(APP_API % appid).get(str(appid), {}).get("data") or {}
    except Exception:
        return None
    m = re.search(r"\b(19|20)\d\d\b", (data.get("release_date") or {}).get("date") or "")
    return int(m.group(0)) if m else None


def site_overrides():
    """The site's platform/status settings ({field: {name: value}}), read-only, or {} if the
    service-account key isn't here. They override the catalog, so the report says where."""
    if not os.path.exists(KEY_FILE):
        return {}
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        firebase_admin.initialize_app(credentials.Certificate(KEY_FILE))
        users = list(firestore.client().collection("users").list_documents())
        return (users[0].collection("state").document("games").get().to_dict() or {}) if len(users) == 1 else {}
    except Exception as e:
        print("(couldn't read the site's settings: %s)" % str(e)[:80])
        return {}


def match(library, master, pinned):
    """{appid: (backlog entry or None, how)} -- how is the match level, "pinned", "year",
    or why it's unsettled ("loose", "ambiguous", "none")."""
    by_core = {}
    for g in master:
        for n in {g["name"], g.get("matchedName") or g["name"]}:
            by_core.setdefault(core(n), []).append(g)
    by_name = {g["name"]: g for g in master}
    out = {}
    for s in library:
        key = str(s["appid"])
        if key in pinned:
            target = pinned[key]
            out[s["appid"]] = (by_name.get(target), "pinned") if target else (None, "pinned")
            continue
        cands = {id(g): g for g in by_core.get(core(s["name"]), [])}.values()
        ranked = {}
        for g in cands:
            lvl = match_level([g["name"], g.get("matchedName") or g["name"]], s["name"])
            if lvl in SURE:
                ranked.setdefault(SURE.index(lvl), []).append(g)
        if ranked:
            best = ranked[min(ranked)]
            if len(best) == 1 and len(cands) == 1:
                out[s["appid"]] = (best[0], SURE[min(ranked)])
                continue
            # Several releases share the name (Resident Evil 4 / 2005): the app's year decides.
            year = release_year(s["appid"])
            time.sleep(0.4)   # the store API allows ~200 calls per 5 minutes
            same_year = [g for g in cands if year and g.get("year") == year]
            out[s["appid"]] = (same_year[0], "year") if len(same_year) == 1 else (None, "ambiguous")
            continue
        # Nothing close by core name: try the looser rules across the whole catalog.
        loose = [g for g in master if match_level([g["name"], g.get("matchedName") or g["name"]], s["name"]) == "loose"]
        out[s["appid"]] = (None, "loose:" + " | ".join(g["name"] for g in loose[:3])) if loose else (None, "none")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="update the master file, then run build.py")
    args = ap.parse_args()

    master = json.load(open(MASTER, encoding="utf-8"))
    pinned = json.load(open(MATCHES, encoding="utf-8")) if os.path.exists(MATCHES) else {}
    library = steam_library()
    print("Steam library: %d games, %.0f hours played" % (len(library), sum(s["minutes"] for s in library) / 60))
    matched = match(library, master, pinned)
    site = site_overrides()
    site_plat, site_prog = site.get("platforms") or {}, site.get("progress") or {}

    add_steam, more_hours, played_backlog, unsettled, not_in_backlog = [], [], [], [], []
    for s in sorted(library, key=lambda x: -x["minutes"]):
        g, how = matched[s["appid"]]
        hours = round(s["minutes"] / 60, 1)
        if g is None:
            (not_in_backlog if how in ("none", "pinned") else unsettled).append((s, how))
            continue
        if "Steam" not in (g.get("platforms") or []):
            add_steam.append((g, s, how))
        if hours > (g.get("playedHours") or 0):
            more_hours.append((g, hours))
        status = site_prog.get(g["name"]) or g.get("progress") or "backlog"
        if hours >= 1 and status == "backlog":
            played_backlog.append((g, hours))
    in_library = {id(g) for g, _how in matched.values() if g is not None}
    missing = [g for g in master if "Steam" in (g.get("platforms") or []) and id(g) not in in_library]

    print("\nGets Steam as a platform (%d):" % len(add_steam))
    for g, s, how in add_steam:
        note = "  [the site's platform setting %s wins]" % site_plat[g["name"]] if g["name"] in site_plat else ""
        print("  %-45s <- Steam \"%s\" (%s)%s" % (g["name"], s["name"], how, note))
    print("\nHours played raised to Steam's playtime (%d):" % len(more_hours))
    for g, hours in more_hours:
        print("  %-45s %s -> %sh" % (g["name"], g.get("playedHours") or 0, hours))
    print("\nPlayed on Steam but still in Backlog -- worth a status on the site (%d):" % len(played_backlog))
    for g, hours in sorted(played_backlog, key=lambda x: -x[1]):
        print("  %-45s %sh" % (g["name"], hours))
    print("\nUnsettled -- pin these in steam_matches.json (%d):" % len(unsettled))
    for s, how in unsettled:
        print("  %-10s %-40s %sh  %s" % (s["appid"], s["name"], round(s["minutes"] / 60, 1), how))
    print("\nOn Steam, not in the backlog (%d; add any you want with the site's + button):" % len(not_in_backlog))
    for s, _how in not_in_backlog:
        print("  %-10s %-40s %sh" % (s["appid"], s["name"], round(s["minutes"] / 60, 1)))
    print("\nMarked Steam in the backlog but not in your Steam library -- left as is (%d):" % len(missing))
    for g in missing:
        print("  " + g["name"])

    if not args.apply:
        print("\nDry run: nothing changed. Run with --apply to update the master file.")
        return
    for g, _s, _how in add_steam:
        g["platforms"] = [p for p in (g.get("platforms") or []) if p != "Wishlist"] + ["Steam"]
        g["owned"] = True
    for g, hours in more_hours:
        g["playedHours"] = hours
    backup = MASTER.replace(".json", ".backup_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    os.replace(MASTER, backup)   # gitignored; the previous master file, in case
    json.dump(master, open(MASTER, "w", encoding="utf-8"), ensure_ascii=False)
    print("\nUpdated %d games (previous master file kept as %s). Rebuilding the page:"
          % (len({id(x[0]) for x in add_steam} | {id(x[0]) for x in more_hours}), os.path.basename(backup)))
    subprocess.run([sys.executable, os.path.join(HERE, "build.py")], check=True)


if __name__ == "__main__":
    main()
