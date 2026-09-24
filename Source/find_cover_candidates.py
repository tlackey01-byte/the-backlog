"""Finds cover art for the games refetch_covers.py couldn't, and lays it out for review.

refetch_covers.py only takes a SteamGridDB game whose name matches exactly, which left 190
games without proper art. Most of them do have art -- filed as "Alan Wake 2" rather than
"II", "Ragnarök" rather than "Ragnarok", "Dishonored" rather than "Dishonored Definitive
Edition" -- and looser matching finds it. But looser matching is also how the wrong game's art
gets in (Steam's "God Hand" is a 2019 indie game, not Capcom's 2006 one), so nothing here is
applied automatically. It gathers candidates from several sources, scores how sure each one
is, and writes a review page; only the choices made on that page get pinned.

Sources, most trustworthy first:
  1. HowLongToBeat -> Steam app id. Each game's matchedName came from HLTB, so HLTB's exact
     hit is the right game, and its page names the Steam app. SteamGridDB's entry for that
     app, and Steam's own art for it, are then the right game's by construction.
  2. SteamGridDB name search with forgiving matching (accents, II = 2, edition suffixes),
     checked against HLTB's release year.
  3. IGDB, if Source/igdb_credentials.json exists ({"clientId": ..., "clientSecret": ...}
     from a Twitch developer app), for console and retro games the other two don't carry.

  # search: only the games named in the file -> cover_review/candidates.json + index.html
  python Source/find_cover_candidates.py --names Source/cover_review/gap_names.txt

  # review it (desktop browser), then "Download decisions"
  python -m http.server 8766 --directory Source/cover_review

  # after editing cover_review/notes.json (first-pass notes shown on the page, and the picks
  # they suggest -- see with_notes()), or re-searching some games with --merge:
  python Source/find_cover_candidates.py --render

  # pin the choices in cover_sources.json and list them for refetch_covers.py --only
  python Source/find_cover_candidates.py --apply-decisions ~/Downloads/cover_decisions.json

In the names file, a "## no-capsule" / "## no-match" / "## no-cover" / "## audit" line sets
the kind of gap for the names under it. "no-capsule" games keep the portrait they already have;
"audit" games (spotted in a review of the rest of the library) keep both images on offer.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from refetch_covers import HERE, IDS_FILE, KEY_FILE, MASTER, PORTRAIT_DIMS, api, norm

REVIEW_DIR = os.path.join(HERE, "cover_review")
IGDB_CREDS = os.path.join(HERE, "igdb_credentials.json")
HLTB = "https://howlongtobeat.com"
# HLTB ties its search token to the caller's User-Agent and turns away obvious scripts; this
# is the same string the worker sends.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
STEAM_ASSETS = "https://shared.cloudflare.steamstatic.com/store_item_assets/"
IGDB_IMG = "https://images.igdb.com/igdb/image/upload/t_1080p/%s.jpg"
# Where the review page shows each game's current covers from -- build.py's COVER_BASE.
COVER_BASE = "https://backlog-proxy.tlackey01.workers.dev/img/"
STEAM_APP_RE = re.compile(r"store\.steampowered\.com/app/(\d+)")

CONF_RANK = {"high": 0, "medium": 1, "low": 2}
PORTRAIT_RANK = {"current": 0, "sgdb": 1, "steam": 2, "igdb": 3}
WIDE_RANK = {"sgdb": 0, "steam": 1, "igdb": 2, "current": 3}


# ---- name matching ----
# refetch_covers.norm() turns every non-ASCII letter into a space, so "Ragnarök" and
# "Ragnarok" never meet, and it knows nothing of "II" = "2" or edition suffixes. These build
# looser keys for the same comparison, each a step less certain than the one before.

SPECIAL = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ł": "l", "đ": "d",
                         "ð": "d", "þ": "th", "×": " x "})  # "HUNTER×HUNTER"
# Lone "i" and "x" are left alone: they're far more often a word or a letter ("I Am Bread",
# "X-Men", "Hunter x Hunter") than a numeral.
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8",
         "ix": "9", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15", "xvi": "16"}
EDITION_TAIL = re.compile(
    r"\s+(?:the\s+)?(?:game of the year|goty|definitive|complete|enhanced|ultimate|deluxe|"
    r"special|anniversary|gold|platinum|collectors|collector s|directors cut|director s cut|"
    r"final cut|remastered|remaster|hd|4k|redux|edition|version|collection|complete adventure|pack)$")


def fold(s):
    """Lower-case, accents off ("ö" -> "o"), roman numerals as digits, "&" as "and"."""
    s = re.sub(r"[™®©]", "", (s or "").lower().translate(SPECIAL))  # before NFKD: it spells ™ "tm"
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    words = re.sub(r"[^a-z0-9]+", " ", s.replace("&", " and ")).split()
    return " ".join(ROMAN.get(w, w) for w in words)


def core(s):
    """fold() with bracketed asides ("(2002)"), edition/version suffixes and a leading "The"
    dropped."""
    k, prev = fold(re.sub(r"\([^)]*\)", " ", s or "")), None
    while k != prev:
        prev, k = k, EDITION_TAIL.sub("", k)
    return re.sub(r"^the\s+", "", k)


def head(s):
    """The part before a subtitle: "Final Fantasy XIV: A Realm Reborn" -> "Final Fantasy XIV"."""
    return re.split(r"\s*:\s*|\s+[-–—]\s+", s or "", maxsplit=1)[0]


def parts(s, subtitles_only=False):
    """core() keys of the pieces a subtitle or brackets split a name into -- "CTR (Crash Team
    Racing)" gives "crash team racing". Short pieces ("CTR", "007") are left out: on their own
    they'd match far too much. subtitles_only skips the first piece, which is usually a
    series name ("Star Wars") that many different games share."""
    bits = re.split(r"\s*[:()]\s*|\s+[-–—]\s+", s or "")
    keys = {core(b) for b in (bits[1:] if subtitles_only else bits)}
    return {k for k in keys if len(k) >= 5}


def singular(k):
    """ "minishoot adventures" -> "minishoot adventure": plurals drift between stores."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in k.split())


def match_level(names, cand):
    """How closely a candidate's name matches any of the game's names, or None.

    exact   the matcher refetch_covers.py uses
    folded  equal once accents, numerals, "&" and spacing are evened out
            ("Mushihime-sama" = "Mushihimesama")
    core    equal once edition suffixes and bracketed asides are dropped too
    loose   one is the other plus words at the end -- except when those start with a number:
            "Fallout 3" is a sequel to "Fallout", not a longer name for it -- or plus words at
            the start ("Disney's Kim Possible: ..."), or one equals a subtitle/bracket piece of
            the other ("CTR (Crash Team Racing)")"""
    if not cand:
        return None
    if any(norm(n) and norm(n) == norm(cand) for n in names):
        return "exact"
    fc = fold(cand)
    if any(fold(n) == fc or fold(n).replace(" ", "") == fc.replace(" ", "") for n in names):
        return "folded"
    cc = core(cand)
    if any(core(n) == cc or singular(core(n)) == singular(cc) for n in names):
        return "core"
    for n in names:
        a = core(n)
        short, longer = sorted((a, cc), key=len)
        rest = longer[len(short) + 1:]
        # "Wasteland 1 - The Original Classic" is still Wasteland; "Fallout 3" isn't Fallout.
        if len(short) >= 4 and longer.startswith(short + " ") and not (rest[:1].isdigit() and rest.split()[0] != "1"):
            return "loose"
        if len(short) >= 8 and longer.endswith(" " + short):
            return "loose"
        if a in parts(cand) or cc in parts(n):
            return "loose"
        # Same subtitle: "007: Blood Stone" and "James Bond: Blood Stone".
        if {k for k in parts(n, True) if len(k) >= 8} & parts(cand, True):
            return "loose"
    return None


def linked_confidence(names, found, conf, why):
    """A Steam app or SteamGridDB entry reached through an id link is usually the game -- but
    not always: HLTB links "The Séance of Blake Manor" to a Steam app called "Eldritch House".
    When the linked entry's name matches none of the game's, drop it to medium so it isn't
    ticked unreviewed, and say what it's called. (Renames trip this too -- Golf Club Nostalgia
    was Golf Club Wasteland -- which is why it's medium, not low.)"""
    if conf == "high" and found and not match_level(names, found):
        return "medium", why + " -- but it's called '%s'" % found
    return conf, why


def confidence(level, year, ref_year):
    """A name match alone is weak evidence -- remakes, ports and same-named indies abound --
    so the release year has to agree (within a year, for regional release gaps) before
    anything counts as high."""
    agree = None if not (year and ref_year) else abs(year - ref_year) <= 1
    if level in ("exact", "folded"):
        return "high" if agree else ("medium" if agree is None else "low")
    return "medium" if agree else "low"


# ---- sources ----

def get(url, data=None, headers=None, tries=4, method=None):
    """Request with backoff on rate limits and server hiccups; returns the body bytes.

    A 429 gets longer and more patient: HLTB rate-limits a library-wide pass hard, and three
    quick retries lost 632 lookups in one run. Its Retry-After is honoured when it sends one."""
    rate_tries = 8
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            return urllib.request.urlopen(req, timeout=45).read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < rate_tries - 1:
                wait = e.headers.get("Retry-After") if e.headers else None
                time.sleep(float(wait) if wait and wait.isdigit() else min(60, 5 * 2 ** attempt))
                attempt += 1
                continue
            if e.code in (500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            raise


class Hltb:
    """Port of hltbInit/hltbSearch/hltbGamePage in worker/src/index.js -- see the notes there.
    The search API is undocumented; if it breaks, the worker's copy breaks too."""

    def __init__(self):
        self.auth = None

    def search(self, q, retried=False):
        if not self.auth or time.time() - self.auth["at"] > 240:
            init = get(HLTB + "/api/search/site/init?t=%d" % int(time.time() * 1000),
                       headers={"User-Agent": BROWSER_UA, "Referer": HLTB + "/"})
            self.auth = dict(json.loads(init), at=time.time())
        a = self.auth
        body = {
            "searchType": "games", "searchTerms": q.split(), "searchPage": 1, "size": 10,
            "searchOptions": {
                "games": {"userId": 0, "platform": "", "sortCategory": "popular", "rangeCategory": "main",
                          "rangeTime": {"min": None, "max": None},
                          "gameplay": {"perspective": "", "flow": "", "genre": "", "difficulty": ""},
                          "rangeYear": {"min": "", "max": ""}, "modifier": ""},
                "users": {"sortCategory": "postcount"}, "lists": {"sortCategory": "follows"},
                "filter": "", "sort": 0, "randomizer": 0},
            "useCache": True}
        headers = {"Content-Type": "application/json", "User-Agent": BROWSER_UA, "Referer": HLTB + "/",
                   "Origin": HLTB, "x-auth-token": a["token"]}
        # The honeypot pair comes and goes (as of 2026-09-23 init sends only a token); echo it
        # back only when it's there.
        if a.get("hpKey"):
            body[a["hpKey"]] = a.get("hpVal")
            headers.update({"x-hp-key": a["hpKey"], "x-hp-val": a.get("hpVal") or ""})
        try:
            out = get(HLTB + "/api/search/site", data=json.dumps(body).encode(), method="POST", headers=headers)
        except urllib.error.HTTPError as e:
            if e.code == 403 and not retried:  # token expired early
                self.auth = None
                return self.search(q, True)
            raise
        return json.loads(out).get("data") or []

    def page(self, game_id):
        html = get(HLTB + "/game/%d" % game_id, headers={"User-Agent": BROWSER_UA}).decode("utf-8", "replace")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
        try:
            return json.loads(m.group(1))["props"]["pageProps"]["game"]["data"]["game"][0]
        except Exception:
            return None


class Igdb:
    """IGDB's v4 API, authenticated as a Twitch app (client-credentials token)."""

    def __init__(self, creds):
        self.client = creds["clientId"]
        tok = json.loads(get("https://id.twitch.tv/oauth2/token?" + urllib.parse.urlencode({
            "client_id": creds["clientId"], "client_secret": creds["clientSecret"],
            "grant_type": "client_credentials"}), data=b"", method="POST"))
        self.token = tok["access_token"]

    def search(self, q):
        q = q.replace('"', " ").replace("\\", " ")
        body = ('search "%s"; fields name,first_release_date,cover.image_id,artworks.image_id,'
                'alternative_names.name,platforms.abbreviation,websites.url,external_games.url; limit 8;' % q)
        out = get("https://api.igdb.com/v4/games", data=body.encode("utf-8"), method="POST", headers={
            "Client-ID": self.client, "Authorization": "Bearer " + self.token, "Accept": "application/json"})
        time.sleep(0.3)  # IGDB allows 4 requests a second
        return json.loads(out)


def steam_art(appid):
    """(app name, portrait url, portrait thumb, header url) from Steam's own store art.

    Asks the store API for the file names rather than guessing /apps/<id>/header.jpg: apps
    from 2025 on keep each asset under a hashed folder, and the guessed path 404s for them."""
    q = {"ids": [{"appid": appid}], "context": {"language": "english", "country_code": "US"},
         "data_request": {"include_assets": True}}
    d = json.loads(get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json=" +
                       urllib.parse.quote(json.dumps(q)), headers={"User-Agent": BROWSER_UA}))
    items = (d.get("response") or {}).get("store_items") or []
    a = (items[0].get("assets") if items else None) or {}
    fmt = a.get("asset_url_format")
    url = lambda f: STEAM_ASSETS + fmt.replace("${FILENAME}", f) if fmt and f else None
    return ((items[0].get("name") if items else None),
            url(a.get("library_capsule_2x") or a.get("library_capsule")), url(a.get("library_capsule")),
            url(a.get("header")))


def sgdb_art(sgdb_id, key, delay):
    """Up to three portrait and three wide grids for one SteamGridDB game, best size first.
    Static and non-NSFW/humor only -- a joke grid is not a cover."""
    grids = api("/grids/game/%d?types=static&nsfw=false&humor=false" % sgdb_id, key).get("data") or []
    time.sleep(delay)
    size = lambda g: (g.get("width"), g.get("height"))
    portraits = [g for dims in PORTRAIT_DIMS for g in grids if size(g) == dims][:3]
    wides = sorted((g for g in grids if size(g) in ((920, 430), (460, 215))), key=lambda g: -g["width"])[:3]
    fmt = lambda g: (g["url"], "%dx%d" % size(g))
    return [fmt(g) for g in portraits], [fmt(g) for g in wides]


def year_of(ts):
    try:
        return time.gmtime(int(ts)).tm_year if ts else None
    except (TypeError, ValueError, OSError):
        return None


# ---- one game ----

YEAR_SUFFIX = re.compile(r"^(.*?)\s*\(((?:19|20)\d\d)\)\s*$")


def hours_gap(g, h):
    """How far an HLTB hit's hours are from the entry's (which were copied from one hit)."""
    pairs = [(g.get("main"), h.get("comp_main")), (g.get("extra"), h.get("comp_plus")),
             (g.get("completionist"), h.get("comp_100"))]
    known = [(a, b / 3600.0) for a, b in pairs if a is not None and b]
    return sum(abs(a - b) for a, b in known) / len(known) if known else float("inf")


def identify(hltb, g):
    """The game's HLTB record: an exact (or accent/numeral-folded) name hit for matchedName,
    then its page for the Steam app id. None if HLTB has nothing that close.

    A matchedName like "Doom (2016)" names one of several same-titled games -- the hours are
    that one's -- so search for "Doom" and keep only the hit from that year. Without this the
    remake/original pairs (Doom, Prey, Resident Evil 2, God of War...) go unidentified, and
    the first refresh gave most of them the other game's art."""
    for q in [g.get("matchedName"), g.get("name")]:
        if not q:
            continue
        m = YEAR_SUFFIX.match(q)
        base, want = (m.group(1), int(m.group(2))) if m else (q, None)
        hits = hltb.search(base)
        time.sleep(0.3)
        same = [h for h in hits if norm(h.get("game_name")) in (norm(q), norm(base))] or \
            [h for h in hits if fold(h.get("game_name")) in (fold(q), fold(base))]
        if want:
            year = lambda h: int(h.get("release_world") or 0)
            same = sorted((h for h in same if abs(year(h) - want) <= 1), key=lambda h: abs(year(h) - want))
        elif len(same) > 1:
            # No year to go on, but the entry's hours came from one of these, so they say which:
            # "Dead Space" with 11/13/20 hours is the 2008 original, not HLTB's first hit (2023).
            same.sort(key=lambda h: hours_gap(g, h))
        pick = same[0] if same else None
        if pick:
            page = hltb.page(pick["game_id"]) or {}
            time.sleep(0.3)
            year = pick.get("release_world") or None
            return {"id": pick["game_id"], "name": pick.get("game_name"),
                    "year": int(year) if year else None,
                    "platforms": pick.get("profile_platform") or page.get("profile_platform") or "",
                    "steam": int(page.get("profile_steam") or 0) or None}
    return None


def gather(g, kind, cached, hltb, igdb, key, delay):
    name = g["name"]
    names = [name] + ([g["matchedName"]] if g.get("matchedName") and g["matchedName"] != name else [])
    row = {"name": name, "kind": kind, "matchedName": g.get("matchedName"),
           "current": {"cover": g.get("cover"), "coverHero": g.get("coverHero")},
           "hltb": None, "steamAppId": None, "portraits": [], "wides": [], "notes": []}
    seen_urls, seen_sgdb = set(), set()

    def add(slot, url, source, conf, why, **extra):
        if url and url not in seen_urls:
            seen_urls.add(url)
            row[slot].append(dict({"url": url, "source": source, "confidence": conf, "why": why}, **extra))

    def add_sgdb(sgdb_id, conf, why):
        if sgdb_id in seen_sgdb:
            return
        seen_sgdb.add(sgdb_id)
        ports, wides = sgdb_art(sgdb_id, key, delay)
        for url, size in ports:
            add("portraits", url, "sgdb", conf, why, sgdbId=sgdb_id, size=size)
        for url, size in wides:
            add("wides", url, "sgdb", conf, why, sgdbId=sgdb_id, size=size)

    h = row["hltb"] = identify(hltb, g)
    ref_year = h and h["year"]
    if h and h["name"] and h["name"] not in names:
        names.append(h["name"])  # HLTB's spelling is one more name the game goes by
    if not ref_year:
        # "Resident Evil (2002)" or a matchedName of "Doom (2016)" says which one it is, even
        # when HLTB can't.
        m = re.search(r"\(((?:19|20)\d\d)\)", name) or re.search(r"\(((?:19|20)\d\d)\)", g.get("matchedName") or "")
        ref_year = int(m.group(1)) if m else None
    appid, appid_conf, appid_why = (h["steam"], "high", "HLTB links Steam app %d" % h["steam"]) \
        if h and h["steam"] else (None, None, None)
    if not h:
        row["notes"].append("no HLTB match" + ("" if ref_year else ", so no release year to check names against"))

    # A no-capsule game's portrait is already right (exact SteamGridDB match); it only
    # needs a wide image. Offer the one it has as the default, and look again on that same
    # SteamGridDB entry in case capsule art has been uploaded since.
    if kind == "no-capsule" and cached.get("url"):
        add("portraits", cached["url"], "current", "high", "the portrait it already has",
            sgdbId=cached.get("sgdbId"), size="600x900")
        if cached.get("sgdbId"):
            add_sgdb(cached["sgdbId"], "high", "SteamGridDB: the entry its portrait came from")
    # An "audit" game already has both images, but one or both looked wrong (another game,
    # a placeholder, blur). Keep them on offer so just the bad one can be swapped -- as
    # medium, so anything found with real evidence outranks them -- and let the searches
    # below re-judge the SteamGridDB entry they came from instead of trusting it.
    if kind == "audit":
        add("portraits", cached.get("url"), "current", "medium", "the portrait it has now",
            sgdbId=cached.get("sgdbId"), size="600x900")
        add("wides", cached.get("wideUrl"), "current", "medium", "the list image it has now")

    # IGDB first: besides its own art, it often knows the Steam app when HLTB doesn't.
    ig = None
    if igdb:
        best = None
        for q in dict.fromkeys(filter(None, [g.get("matchedName"), h and h["name"], name])):
            for c in igdb.search(q):
                lvl = min((match_level(names, n) for n in [c.get("name")] +
                           [a.get("name") for a in c.get("alternative_names") or []]),
                          key=lambda l: ("exact", "folded", "core", "loose", None).index(l))
                if not lvl:
                    continue
                conf = confidence(lvl, year_of(c.get("first_release_date")), ref_year)
                score = (CONF_RANK[conf], ("exact", "folded", "core", "loose").index(lvl))
                if best is None or score < best[0]:
                    best = (score, c, lvl, conf)
            if best and best[0] == (0, 0):
                break
        if best:
            _, c, lvl, conf = best
            urls = [w.get("url") or "" for w in (c.get("websites") or []) + (c.get("external_games") or [])]
            steam = next((int(m.group(1)) for u in urls for m in [STEAM_APP_RE.search(u)] if m), None)
            ig = {"name": c.get("name"), "year": year_of(c.get("first_release_date")), "level": lvl,
                  "conf": conf, "cover": (c.get("cover") or {}).get("image_id"),
                  "artworks": [a["image_id"] for a in c.get("artworks") or [] if a.get("image_id")][:2],
                  "platforms": ", ".join(p.get("abbreviation") or "" for p in c.get("platforms") or []),
                  "steam": steam}
            row["igdb"] = {k: ig[k] for k in ("name", "year", "level", "platforms", "steam")}
            if not appid and steam:
                appid, appid_conf = steam, conf
                appid_why = "IGDB (%s match on %s) links Steam app %d" % (lvl, c.get("name"), steam)

    if appid:
        row["steamAppId"] = appid
        try:
            sg = api("/games/steam/%d" % appid, key).get("data")
        except urllib.error.HTTPError:
            sg = None  # SteamGridDB doesn't know this app
        time.sleep(delay)
        if sg and sg.get("id"):
            add_sgdb(sg["id"], *linked_confidence(names, sg.get("name"), appid_conf,
                                                  "SteamGridDB '%s', via %s" % (sg.get("name"), appid_why)))
        try:
            steam_name, port, port_thumb, hdr = steam_art(appid)
        except (urllib.error.URLError, OSError, ValueError):
            steam_name = port = port_thumb = hdr = None  # delisted app, or the store API hiccuped
            row["notes"].append("Steam's store API had nothing for app %d" % appid)
        conf, why = linked_confidence(names, steam_name, appid_conf, "via " + appid_why)
        add("portraits", port, "steam", conf, "Steam's library art, " + why, size="600x900", thumb=port_thumb)
        add("wides", hdr, "steam", conf, "Steam's store header, " + why, size="460x215")

    # Name search on SteamGridDB -- where most of the misses are, because it's the same
    # search refetch_covers.py ran, now matched forgivingly. A no-capsule game only gets it
    # if nothing above turned up a wide image.
    if kind != "no-capsule" or not row["wides"]:
        found = {}
        queries = [name, g.get("matchedName"), h and h["name"], head(name), core(name)]
        for q in [q for q in dict.fromkeys(filter(None, queries)) if len(q) >= 3][:5]:
            for c in api("/search/autocomplete/" + urllib.parse.quote(q, safe=""), key).get("data") or []:
                lvl = match_level(names, c.get("name"))
                if lvl and c["id"] not in seen_sgdb:
                    conf = confidence(lvl, year_of(c.get("release_date")), ref_year)
                    score = (CONF_RANK[conf], ("exact", "folded", "core", "loose").index(lvl))
                    if c["id"] not in found or score < found[c["id"]][0]:
                        found[c["id"]] = (score, c, lvl, conf)
            time.sleep(delay)
        for score, c, lvl, conf in sorted(found.values(), key=lambda x: x[0])[:2]:
            y = year_of(c.get("release_date"))
            add_sgdb(c["id"], conf, "SteamGridDB '%s'%s, %s name match%s" % (
                c.get("name"), " (%d)" % y if y else "", lvl,
                "" if not (y and ref_year) else (", year agrees" if abs(y - ref_year) <= 1 else
                                                 ", but HLTB says %d" % ref_year)))

    if ig:
        why = "IGDB '%s'%s, %s name match" % (ig["name"], " (%d)" % ig["year"] if ig["year"] else "", ig["level"])
        if ig["cover"]:
            add("portraits", IGDB_IMG % ig["cover"], "igdb", ig["conf"], why, size="~3:4, cropped")
        for a in ig["artworks"]:
            add("wides", IGDB_IMG % a, "igdb", ig["conf"], why + " (artwork, cropped)", size="16:9, cropped")

    row["portraits"].sort(key=lambda c: (CONF_RANK[c["confidence"]], PORTRAIT_RANK[c["source"]]))
    row["wides"].sort(key=lambda c: (CONF_RANK[c["confidence"]], WIDE_RANK[c["source"]]))
    return row


# ---- review page ----

def thumb(url, portrait):
    """A small stand-in for the review page. The real images run to ~1MB of PNG each, and a
    page of 190 games showing several apiece stalls the browser decoding them."""
    u = re.sub(r"(steamgriddb\.com)/grid/([0-9a-f]+)\.\w+$", r"\1/thumb/\2.jpg", url)
    u = u.replace("/library_600x900_2x.jpg", "/library_600x900.jpg")
    return u.replace("/t_1080p/", "/t_cover_big/" if portrait else "/t_screenshot_med/")


def for_display(r, per_slot=6, keep=()):
    """What the page shows of one row: low-confidence alternates only when nothing better
    turned up (next to a sure match they're just franchise noise -- "God of War" 2005 beside
    Ragnarök), at most `per_slot` a slot, and each with a thumbnail. Urls in `keep` -- ones
    a review note picks, like the original Cotton Reboot! behind its sequel's matches -- are
    always shown. Indexes into the lists shown are what the page exports, so the full
    candidates.json stays as found."""
    out = dict(r)
    for slot in ("portraits", "wides"):
        cs = r[slot]
        if any(c["confidence"] == "high" for c in cs):
            cs = [c for c in cs if c["confidence"] != "low" or c["url"] in keep]
        cs = cs[:per_slot] + [c for c in cs[per_slot:] if c["url"] in keep]
        out[slot] = [dict(c, thumb=c.get("thumb") or thumb(c["url"], slot == "portraits")) for c in cs]
    return out


def with_notes(r, note):
    """Attach a reviewer's note from notes.json: {"note": ..., "p": <portrait url>,
    "w": <wide url> | "fallback", "on": true|false}. Picks are stored by url rather than by
    position so they survive a re-search that reorders or adds candidates."""
    pick = {}
    purls, wurls = [c["url"] for c in r["portraits"]], [c["url"] for c in r["wides"]]
    if note.get("p") in purls:
        pick["p"] = purls.index(note["p"])
    if note.get("w") == "fallback":
        pick["w"] = "fallback"
    elif note.get("w") in wurls:
        pick["w"] = wurls.index(note["w"])
    if "on" in note:
        pick["on"] = note["on"]
    return dict(r, reviewNote={"note": note.get("note"), "pick": pick})


def render(rows, path):
    notes_path = os.path.join(REVIEW_DIR, "notes.json")
    notes = json.load(open(notes_path, encoding="utf-8")) if os.path.exists(notes_path) else {}
    picked = lambda n: {notes.get(n, {}).get("p"), notes.get(n, {}).get("w")}
    rows = [for_display(r, keep=picked(r["name"])) for r in rows]
    rows = [with_notes(r, notes[r["name"]]) if r["name"] in notes else r for r in rows]
    data = json.dumps({"rows": rows, "coverBase": COVER_BASE}, ensure_ascii=False).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(REVIEW_HTML.replace("__DATA__", data))


def apply_decisions(path):
    """Pins each approved choice in cover_sources.json and lists the games for --only."""
    dec = json.load(open(os.path.expanduser(path), encoding="utf-8"))
    ids = json.load(open(IDS_FILE, encoding="utf-8"))
    known = {g["name"] for g in json.load(open(MASTER, encoding="utf-8"))}
    unknown = sorted(set(dec["games"]) - known)
    if unknown:
        sys.exit("not in the master file: " + "; ".join(unknown))
    os.makedirs(REVIEW_DIR, exist_ok=True)
    backup = os.path.join(REVIEW_DIR, "cover_sources.before_decisions.json")
    shutil.copy(IDS_FILE, backup)
    names = []
    for name, d in dec["games"].items():
        p, w = d["portrait"], d.get("wide")
        old = ids.get(name) or {}
        src = old.get("source", "sgdb") if p["source"] == "current" else p["source"]
        # refetch_covers.py reads url/wideUrl as given; sgdbId only matters when there's no url.
        # A present-but-null wideUrl is what tells it to use the blurred fallback.
        ids[name] = {"sgdbId": p.get("sgdbId") if src == "sgdb" else None, "url": p["url"],
                     "wideUrl": w["url"] if w else None, "source": src,
                     "wideSource": w["source"] if w else "fallback", "pinned": True}
        names.append(name)
    json.dump(ids, open(IDS_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    out = os.path.join(REVIEW_DIR, "apply_names.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(names) + "\n")
    print("pinned %d games in %s (previous copy: %s)" % (len(names), os.path.basename(IDS_FILE), backup))
    print("next: python Source/refetch_covers.py --apply --only %s" % os.path.relpath(out))


def read_gap_file(path):
    """[(name, kind)] from a names file whose "## kind" lines label the names below them."""
    out, kind = [], "no-match"
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if s.startswith("## "):
                kind = s[3:].strip()
            elif s and not s.startswith("#"):
                out.append((s, kind))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", help="file of game names to search for (see the docstring)")
    ap.add_argument("--render", action="store_true", help="rebuild index.html from candidates.json only")
    ap.add_argument("--apply-decisions", metavar="FILE", help="pin the choices exported from the review page")
    ap.add_argument("--delay", type=float, default=0.25, help="seconds between SteamGridDB calls")
    ap.add_argument("--merge", action="store_true",
                    help="replace just these games' rows in the existing candidates.json, keeping the rest")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cand_path = os.path.join(REVIEW_DIR, "candidates.json")
    html_path = os.path.join(REVIEW_DIR, "index.html")
    if args.apply_decisions:
        return apply_decisions(args.apply_decisions)
    if args.render:
        render(json.load(open(cand_path, encoding="utf-8")), html_path)
        return print("wrote " + html_path)
    if not args.names:
        sys.exit("give --names FILE, --render, or --apply-decisions FILE")

    wanted = read_gap_file(args.names)
    games = {g["name"]: g for g in json.load(open(MASTER, encoding="utf-8"))}
    unknown = [n for n, _ in wanted if n not in games]
    if unknown:
        sys.exit("not in the master file: " + "; ".join(unknown))
    ids = json.load(open(IDS_FILE, encoding="utf-8")) if os.path.exists(IDS_FILE) else {}
    key = open(KEY_FILE, encoding="utf-8").read().strip()
    igdb = None
    if os.path.exists(IGDB_CREDS):
        igdb = Igdb(json.load(open(IGDB_CREDS, encoding="utf-8")))
    else:
        print("no %s -- skipping IGDB\n" % os.path.basename(IGDB_CREDS))
    hltb = Hltb()
    os.makedirs(REVIEW_DIR, exist_ok=True)
    if args.merge and os.path.exists(cand_path):
        # The checkpoints below overwrite candidates.json with just this run's rows.
        shutil.copy(cand_path, cand_path + ".before_merge")

    rows = []
    for n, (name, kind) in enumerate(wanted, 1):
        try:
            row = gather(games[name], kind, ids.get(name) or {}, hltb, igdb, key, args.delay)
        except Exception as e:
            row = {"name": name, "kind": kind, "matchedName": games[name].get("matchedName"),
                   "current": {"cover": games[name].get("cover"), "coverHero": games[name].get("coverHero")},
                   "hltb": None, "steamAppId": None, "portraits": [], "wides": [],
                   "notes": ["ERROR " + str(e)[:160]]}
        rows.append(row)
        p, w = row["portraits"], row["wides"]
        print("%3d/%d  %-44.44s %-10s portrait %-6s wide %-6s %s" % (
            n, len(wanted), name, kind, p[0]["confidence"] + "/" + p[0]["source"] if p else "-",
            w[0]["confidence"] + "/" + w[0]["source"] if w else "-",
            "; ".join(x for x in row["notes"] if x.startswith("ERROR"))), flush=True)
        if n % 20 == 0:
            json.dump(rows, open(cand_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)

    if args.merge and os.path.exists(cand_path + ".before_merge"):
        # Splice the fresh rows over the old ones, keeping the old file's order, so a re-run
        # of the weak games (say, once IGDB is set up) doesn't drop the rest of the list.
        fresh = {r["name"]: r for r in rows}
        old = json.load(open(cand_path + ".before_merge", encoding="utf-8"))
        rows = [fresh.pop(r["name"], r) for r in old] + list(fresh.values())
    json.dump(rows, open(cand_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    render(rows, html_path)

    def best(r, slot):
        return r[slot][0]["confidence"] if r[slot] else "none"
    print("\n%d games -> %s" % (len(rows), html_path))
    for kind in dict.fromkeys(k for _, k in wanted):
        rs = [r for r in rows if r["kind"] == kind]
        slot = "wides" if kind == "no-capsule" else "portraits"
        counts = {c: sum(1 for r in rs if best(r, slot) == c) for c in ("high", "medium", "low", "none")}
        print("  %-10s %3d  best %s: %s" % (kind, len(rs), slot[:-1],
                                            "  ".join("%s %d" % kv for kv in counts.items())))
    errors = sum(1 for r in rows if any(x.startswith("ERROR") for x in r["notes"]))
    print("  errors: %d" % errors)


REVIEW_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cover Review</title>
<style>
  :root { --bg:#14161a; --panel:#1d2026; --line:#2c3038; --text:#e6e8eb; --dim:#9aa1ab;
          --accent:#f28c28; --high:#3fb96b; --medium:#e0a526; --low:#e0524f; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:14px/1.4 system-ui, sans-serif; }
  header { position:sticky; top:0; z-index:5; background:var(--panel); border-bottom:1px solid var(--line);
           padding:10px 16px; display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; }
  header h1 { font-size:16px; margin:0 8px 0 0; }
  header .count { color:var(--dim); }
  select, button, input[type=text] { font:inherit; color:var(--text); background:var(--bg);
           border:1px solid var(--line); border-radius:6px; padding:5px 8px; }
  button { cursor:pointer; }
  button.primary { background:var(--accent); border-color:var(--accent); color:#1a1206; font-weight:600; }
  main { padding:12px 16px 60px; max-width:1400px; margin:0 auto; }
  .row { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px; margin:0 0 12px; }
  .row.off { opacity:.55; }
  .top { display:flex; flex-wrap:wrap; gap:6px 12px; align-items:baseline; margin-bottom:8px; }
  .top label { font-weight:600; font-size:15px; display:flex; gap:8px; align-items:center; cursor:pointer; }
  .top input { width:18px; height:18px; accent-color:var(--accent); }
  .meta { color:var(--dim); }
  .meta a { color:var(--dim); }
  .tag { font-size:11px; padding:1px 7px; border-radius:99px; border:1px solid var(--line); color:var(--dim); }
  .note { color:var(--medium); font-size:12px; }
  .note.reviewed { color:#8fb8ff; }
  .strip { display:grid; grid-template-columns:70px 1fr; gap:6px 10px; align-items:start; margin-top:6px; }
  .strip > .lbl { color:var(--dim); font-size:12px; padding-top:4px; }
  .opts { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-start; }
  .opt { position:relative; border:2px solid transparent; border-radius:6px; padding:2px; cursor:pointer;
         background:var(--bg); display:flex; flex-direction:column; align-items:center; gap:3px; }
  .opt.sel { border-color:var(--accent); }
  .opt img { display:block; border-radius:3px; background:#0d0f12; object-fit:cover; }
  .opt.p img { width:80px; height:120px; }
  .opt.w img { width:184px; height:86px; }
  .opt.fb { width:184px; height:86px; justify-content:center; color:var(--dim); font-size:12px; text-align:center; white-space:pre-line; }
  .badge { font-size:10px; line-height:1; padding:3px 5px; border-radius:4px; color:#111; font-weight:700; }
  .badge.high { background:var(--high); } .badge.medium { background:var(--medium); } .badge.low { background:var(--low); }
  .badge.manual { background:#8fb8ff; }
  .cur img { opacity:.9; }
  .custom { display:flex; flex-direction:column; gap:4px; width:220px; }
  .custom input { width:100%; font-size:12px; }
  .empty { color:var(--dim); font-size:12px; padding-top:4px; }
  @media (max-width:700px) { .strip { grid-template-columns:1fr; } .opt.w img, .opt.fb { width:138px; height:65px; } }
</style>
</head>
<body>
<header>
  <h1>Cover review</h1>
  <span class="count" id="count"></span>
  <select id="fkind"><option value="">All kinds</option><option>no-match</option><option>no-capsule</option><option>no-cover</option><option>audit</option></select>
  <select id="fshow"><option value="">All games</option><option value="on">Ticked</option><option value="off">Unticked</option>
    <option value="unsure">Not high confidence</option><option value="noted">Has a first-pass note</option><option value="none">Nothing found</option></select>
  <button id="reset">Reset to suggestions</button>
  <button id="dl" class="primary">Download decisions</button>
</header>
<main id="list"></main>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var D = JSON.parse(document.getElementById('data').textContent);
  var rows = D.rows, KEY = 'cover-review-v1', state = {};
  var list = document.getElementById('list');

  // Suggested choice: best-ranked candidate in each slot (the script sorts them), ticked only
  // when everything it would change is high confidence. A game with nothing found starts off.
  function suggest(r) {
    var p = r.portraits.length ? 0 : null, w = r.wides.length ? 0 : 'fallback';
    var sure = p !== null && r.portraits[0].confidence === 'high' &&
               (w === 'fallback' || r.wides[0].confidence === 'high');
    if (r.kind === 'no-capsule') sure = sure && w !== 'fallback';
    if (r.reviewNote) { var c = r.reviewNote.pick; if ('p' in c) p = c.p; if ('w' in c) w = c.w; if ('on' in c) sure = c.on; }
    return { on: !!sure, p: p, pc: '', w: w, wc: '' };
  }
  function load() {
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
    rows.forEach(function (r) { state[r.name] = saved[r.name] || suggest(r); });
  }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  // `full` is tried if `src` fails: a few SteamGridDB grids have no thumbnail.
  function img(src, w, h, full) {
    var i = el('img'); i.loading = 'lazy'; i.width = w; i.height = h; i.alt = '';
    if (full && full !== src) i.onerror = function () { i.onerror = null; i.src = full; };
    i.src = src; return i;
  }
  function badge(c) { var b = el('span', 'badge ' + c.confidence, c.source + ' · ' + c.confidence); return b; }

  function option(kind, c, selected, onpick) {
    var o = el('div', 'opt ' + kind + (selected ? ' sel' : ''));
    o.title = (c.why || '') + (c.size ? ' — ' + c.size : '');
    o.appendChild(img(c.thumb || c.url, kind === 'p' ? 80 : 184, kind === 'p' ? 120 : 86, c.url));
    o.appendChild(badge(c));
    o.onclick = onpick;
    return o;
  }

  function custom(kind, value, onchange) {
    var box = el('div', 'custom');
    var inp = el('input'); inp.type = 'text'; inp.placeholder = 'or paste an image URL'; inp.value = value || '';
    var prev = el('div');
    function show() { prev.innerHTML = ''; if (inp.value.trim()) prev.appendChild(img(inp.value.trim(), kind === 'p' ? 80 : 184, kind === 'p' ? 120 : 86)); }
    inp.onchange = function () { onchange(inp.value.trim()); show(); };
    box.appendChild(inp); box.appendChild(prev); show();
    return box;
  }

  function renderRow(r) {
    var s = state[r.name], div = el('div', 'row' + (s.on ? '' : ' off'));
    div.dataset.name = r.name;
    var top = el('div', 'top'), lab = el('label'), cb = el('input');
    cb.type = 'checkbox'; cb.checked = s.on;
    cb.onchange = function () { s.on = cb.checked; save(); refresh(r); };
    lab.appendChild(cb); lab.appendChild(document.createTextNode(r.name)); top.appendChild(lab);
    top.appendChild(el('span', 'tag', r.kind));
    var meta = [];
    if (r.hltb) meta.push('HLTB: ' + r.hltb.name + (r.hltb.year ? ' (' + r.hltb.year + ')' : '') + (r.hltb.platforms ? ' · ' + r.hltb.platforms : ''));
    if (r.igdb) meta.push('IGDB: ' + r.igdb.name + (r.igdb.year ? ' (' + r.igdb.year + ')' : ''));
    var m = el('span', 'meta', meta.join('  ·  ')); top.appendChild(m);
    if (r.steamAppId) { var a = el('a', null, 'Steam ' + r.steamAppId); a.href = 'https://store.steampowered.com/app/' + r.steamAppId; a.target = '_blank'; m.appendChild(document.createTextNode('  ·  ')); m.appendChild(a); }
    div.appendChild(top);
    (r.notes || []).forEach(function (n) { div.appendChild(el('div', 'note', n)); });
    if (r.reviewNote && r.reviewNote.note) div.appendChild(el('div', 'note reviewed', 'First pass: ' + r.reviewNote.note));

    var strip = el('div', 'strip');
    strip.appendChild(el('div', 'lbl', 'Now'));
    var cur = el('div', 'opts cur');
    if (r.current.cover) cur.appendChild(img(D.coverBase + r.current.cover, 184, 86));
    if (r.current.coverHero) cur.appendChild(img(D.coverBase + r.current.coverHero, 57, 86));
    if (!r.current.cover) cur.appendChild(el('span', 'empty', 'no cover'));
    strip.appendChild(cur);

    strip.appendChild(el('div', 'lbl', 'Portrait'));
    var po = el('div', 'opts');
    r.portraits.forEach(function (c, i) { po.appendChild(option('p', c, s.p === i, function () { s.p = i; s.on = true; save(); refresh(r); })); });
    if (!r.portraits.length) po.appendChild(el('span', 'empty', 'nothing found'));
    po.appendChild(custom('p', s.pc, function (v) { s.pc = v; if (v) { s.p = 'custom'; s.on = true; } else if (s.p === 'custom') s.p = r.portraits.length ? 0 : null; save(); refresh(r); }));
    strip.appendChild(po);

    strip.appendChild(el('div', 'lbl', 'Wide'));
    var wo = el('div', 'opts');
    r.wides.forEach(function (c, i) { wo.appendChild(option('w', c, s.w === i, function () { s.w = i; s.on = true; save(); refresh(r); })); });
    var fb = el('div', 'opt fb' + (s.w === 'fallback' ? ' sel' : ''), 'blurred fallback\n(made from the portrait)');
    fb.onclick = function () { s.w = 'fallback'; save(); refresh(r); };
    wo.appendChild(fb);
    wo.appendChild(custom('w', s.wc, function (v) { s.wc = v; if (v) { s.w = 'custom'; s.on = true; } else if (s.w === 'custom') s.w = r.wides.length ? 0 : 'fallback'; save(); refresh(r); }));
    strip.appendChild(wo);
    div.appendChild(strip);
    return div;
  }

  function visible(r) {
    var s = state[r.name], k = document.getElementById('fkind').value, f = document.getElementById('fshow').value;
    if (k && r.kind !== k) return false;
    if (f === 'on') return s.on;
    if (f === 'off') return !s.on;
    if (f === 'none') return !r.portraits.length && !r.wides.length;
    if (f === 'noted') return !!r.reviewNote;
    if (f === 'unsure') return !(r.portraits.length && r.portraits[0].confidence === 'high');
    return true;
  }
  function refresh(r) {
    var old = list.querySelector('[data-name="' + CSS.escape(r.name) + '"]');
    if (old) old.replaceWith(renderRow(r));
    count();
  }
  function count() {
    var on = rows.filter(function (r) { return state[r.name].on; }).length;
    document.getElementById('count').textContent = on + ' of ' + rows.length + ' ticked';
  }
  function renderAll() {
    list.innerHTML = '';
    rows.forEach(function (r) { if (visible(r)) list.appendChild(renderRow(r)); });
    count();
  }

  function decisions() {
    var out = {}, skipped = [];
    rows.forEach(function (r) {
      var s = state[r.name];
      if (!s.on) return;
      var p = s.p === 'custom' ? { url: s.pc, source: 'manual' } : (s.p === null ? null : r.portraits[s.p]);
      var w = s.w === 'custom' ? { url: s.wc, source: 'manual' } : (s.w === 'fallback' ? null : r.wides[s.w]);
      if (!p || !p.url) { skipped.push(r.name); return; }
      out[r.name] = { portrait: p, wide: w };
    });
    return { out: out, skipped: skipped };
  }

  document.getElementById('dl').onclick = function () {
    var d = decisions();
    var blob = new Blob([JSON.stringify({ version: 1, games: d.out }, null, 1)], { type: 'application/json' });
    var a = el('a'); a.href = URL.createObjectURL(blob); a.download = 'cover_decisions.json';
    document.body.appendChild(a); a.click(); a.remove();
    var msg = Object.keys(d.out).length + ' games saved to cover_decisions.json';
    if (d.skipped.length) msg += ' — skipped (ticked but no portrait chosen): ' + d.skipped.join(', ');
    document.getElementById('count').textContent = msg;
  };
  document.getElementById('reset').onclick = function () {
    rows.forEach(function (r) { state[r.name] = suggest(r); }); save(); renderAll();
  };
  document.getElementById('fkind').onchange = renderAll;
  document.getElementById('fshow').onchange = renderAll;
  load(); renderAll();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
