"""Brings the site's own changes into the repo: games added and deleted on the site.

Games added with the site's "+ Add Game" button live in Firestore (users/{uid}/addedGames),
and deleting a catalog game on the site only flags it (the `deleted` list in
users/{uid}/state/games) -- the site is a static page and can't edit the master file. This
reads Firestore directly (no exported backup needed) and folds both into the master file:

  - adds each site-added game that isn't in the master file yet. Its covers are already in
    the bucket's added/ folder (the site uploads them when the game is added); they're copied
    to the normal location inside R2. Games added before that existed carry only an embedded
    portrait, and get their covers built by refetch_covers.py instead.
  - once a baked game's push is live, deletes its Firestore record and its added/ originals.
    Not in the same run as the bake: until the push is live, the live page still draws the
    game from that record, covers and all.
  - removes games deleted on the site from the master file, and finishes any site-added
    delete the page didn't get to (its tab closed during the Undo window, say).
  - deletes every cover in the bucket that nothing uses any more, then rebuilds the page.

It is the only thing that deletes covers outside added/. Firestore's state document (status,
hours, journal...) is never written: an open page saves that whole document back, so edits
made here would just be undone. Leftover entries for removed games are harmless -- the page
ignores names it doesn't know.

Needs Source/firebase_service_account.json (Firebase console -> Project settings -> Service
accounts -> Generate new private key; gitignored -- it has full admin access to the project)
and `pip install firebase-admin`, plus R2 credentials (see upload_covers.py).

  python Source/sync_site.py --dry-run   # show what a run would do, change nothing
  python Source/sync_site.py             # do it, then commit + push
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MASTER = os.path.join(HERE, "master_games_final.json")
IDS_FILE = os.path.join(HERE, "cover_sources.json")
KEY_FILE = os.path.join(HERE, "firebase_service_account.json")
NAMES_FILE = os.path.join(HERE, "cover_review", "synced_names.txt")

from build import COVER_BASE, COVERS_DIR, cover_name  # noqa: E402  (page URL prefix; local copies)
from upload_covers import local_files, r2_client, remote_keys, upload  # noqa: E402

PAGES_RUNS = "https://api.github.com/repos/tlackey01-byte/the-backlog/actions/runs?head_sha=%s"
# More deletions than this in one run is unusual enough to ask first (--yes skips asking).
PRUNE_CONFIRM_OVER = 50


# ---- reading the site's state ----

def connect():
    """Firestore client and the site owner's users/{uid} document. The site has exactly one
    user; more than one would mean this can't know whose games to fold in, so it stops."""
    if not os.path.exists(KEY_FILE):
        sys.exit("missing %s -- see this file's docstring" % KEY_FILE)
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        sys.exit("firebase-admin is not installed: pip install firebase-admin")
    firebase_admin.initialize_app(credentials.Certificate(KEY_FILE))
    db = firestore.client()
    users = list(db.collection("users").list_documents())
    if len(users) != 1:
        sys.exit("expected exactly one user in Firestore, found %d" % len(users))
    return db, users[0]


def read_site(user):
    """(names flagged deleted, {doc id: added-game record})."""
    state = user.collection("state").document("games").get().to_dict() or {}
    added = {d.id: d.to_dict() for d in user.collection("addedGames").stream()}
    return set(state.get("deleted") or []), added


def live_master():
    """The master file the live site was built from: origin/main's, fetched fresh."""
    fetched = subprocess.run(["git", "-C", REPO, "fetch", "-q", "origin"], capture_output=True, text=True)
    out = subprocess.run(["git", "-C", REPO, "show", "origin/main:Source/master_games_final.json"],
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        sys.exit("can't read origin/main's master file: " + out.stderr.strip())
    sha = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "origin/main"],
                         capture_output=True, text=True).stdout.strip()
    return json.loads(out.stdout), sha, fetched.returncode == 0


def added_key(url):
    """The bucket key behind a cover URL, if it's one of the site's own (under added/)."""
    url = url or ""
    return url[len(COVER_BASE):] if url.startswith(COVER_BASE + "added/") else None


def added_keys(rec):
    """The bucket keys of a site-added game's own covers: cv = list image, hv = hero."""
    return [k for k in (added_key(rec.get("cv")), added_key(rec.get("hv"))) if k]


# ---- adding site games to the master file ----

def compact_to_master(r):
    """A site-added game's Firestore record (build.py's compact shape) as a master-file entry.
    Covers are handled by add_games(), not here."""
    genres = r.get("g") or []
    # The master file stores "no platforms" as [] -- the page re-derives the 'Wishlist' default.
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
    if r.get("dv"):
        rec["developer"] = r["dv"]
    # Which release it is (see identify_games.py) -- the page and build.py's art check rely on it.
    if r.get("h") or r.get("hltbId"):
        rec["hltbId"] = r.get("h") or r.get("hltbId")
    if r.get("y"):
        rec["year"] = r["y"]
    return rec


_hltb = None


def fill_from_hltb(rec):
    """Release year and developer from the game's HLTB page when the record lacks them --
    games added before the site recorded those. The year matters most: refetch_covers.py
    uses it to turn down another release's art ("Doom" 1993 vs 2016)."""
    global _hltb
    if not rec.get("hltbId") or (rec.get("year") and rec.get("developer")):
        return
    from find_cover_candidates import Hltb
    _hltb = _hltb or Hltb()
    try:
        page = _hltb.page(int(rec["hltbId"])) or {}
    except Exception as e:
        print("  (couldn't read HLTB page for %s: %s)" % (rec["name"], str(e)[:80]))
        return
    released = str(page.get("release_world") or "")
    if not rec.get("year") and released[:4].isdigit() and int(released[:4]) > 1950:
        rec["year"] = int(released[:4])
    if not rec.get("developer") and (page.get("profile_dev") or "").strip():
        rec["developer"] = page["profile_dev"].strip()


def add_games(adds, master, ids, s3, bucket):
    """Append each site-added game to the master file. Returns the names whose covers still
    need building by refetch_covers.py (games added before the site uploaded covers).

    Status, hours and platforms changed on the site since adding stay in Firestore's state
    document, keyed by name, and keep applying on top of these defaults -- nothing to merge."""
    legacy = []
    for _doc_id, r in adds:
        rec = compact_to_master(r)
        fill_from_hltb(rec)
        wide, hero = added_key(r.get("cv")), added_key(r.get("hv"))
        if wide and hero:
            # Copied, not moved: the live page keeps drawing this game from its Firestore
            # record (and so from added/) until the push is live; finish_handoffs() removes
            # the originals on a later run. Same bytes, same name, just outside added/.
            for key in (wide, hero):
                dest = key[len("added/"):]
                s3.copy_object(Bucket=bucket, Key=dest, CopySource={"Bucket": bucket, "Key": key})
                local = os.path.join(COVERS_DIR, dest)
                os.makedirs(os.path.dirname(local), exist_ok=True)
                s3.download_file(bucket, dest, local)
            rec["cover"] = wide[len("added/"):]
            rec["coverHero"] = hero[len("added/"):]
        else:
            # Only an embedded portrait. It stays as the cover (build.py writes it out as a
            # file) in case refetch_covers.py finds nothing better.
            if r.get("cv"):
                rec["cover"] = r["cv"]
            legacy.append(rec["name"])
        # Where its art came from, so a later refetch can rebuild it from the same source.
        if r.get("cs") and rec["name"] not in ids:
            ids[rec["name"]] = {"sgdbId": r.get("sgdbId"), "url": r["cs"], "wideUrl": r.get("ws"),
                                "source": "site", "pinned": True}
        master.append(rec)
        print("added:", rec["name"])
    return legacy


def build_legacy_covers(names):
    """Covers for older site-added games, the way the bake step did it: refetch_covers.py from
    the pinned source, or a year-checked name search. It uploads what it builds."""
    os.makedirs(os.path.dirname(NAMES_FILE), exist_ok=True)
    with open(NAMES_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(names) + "\n")
    subprocess.run([sys.executable, os.path.join(HERE, "refetch_covers.py"), "--apply", "--only", NAMES_FILE],
                   check=True)


# ---- removing site records: finished handoffs and site deletes ----

def remove_site_records(items, added, user, s3, bucket):
    """Delete each record's own added/ covers, then the Firestore record itself.

    Used for games whose handoff is done (baked, and the push that bakes them is live) and
    for site-added games deleted on the site that the page didn't finish removing. Identical
    images share one object (names are content hashes), so a cover another remaining record
    still uses is left alone."""
    removing = {doc_id for doc_id, _r in items}
    still_used = {k for doc_id, r in added.items() if doc_id not in removing for k in added_keys(r)}
    col = user.collection("addedGames")
    gone = set()
    for doc_id, r in items:
        keys = [k for k in added_keys(r) if k not in still_used and k not in gone]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys]})
            gone.update(keys)
        col.document(doc_id).delete()
        print("removed site record: %s (%d added/ cover%s)" % (r.get("n"), len(keys), "" if len(keys) == 1 else "s"))


# ---- games deleted on the site ----

def remove_catalog_games(names, master, ids):
    """Drop games deleted on the site from the master file and their cover pins. Their cover
    files go in prune_covers(), once nothing references them. Permanent from here: git
    history is the only way back (the master file is backed up first, for this run)."""
    names = set(names)
    master[:] = [g for g in master if g["name"] not in names]
    for name in sorted(names):
        ids.pop(name, None)
        print("removed from the master file:", name)


# ---- deleting covers nothing uses ----

def cover_refs(games):
    """Every bucket key these games' covers point at. An embedded (base64) cover counts under
    the file name build.py will write it out as."""
    refs = set()
    for g in games:
        c = g.get("cover")
        if c:
            refs.add(cover_name(c)[0] if c.startswith("data:") else c)
        if g.get("coverHero"):
            refs.add(g["coverHero"])
    return refs


def unused_covers(new_master, live, deleted, remote):
    """Keys outside added/ that neither the new master file nor the live site needs.

    The live site's covers are kept even when the new master file doesn't use them -- a cover
    replaced but not pushed yet, a game renamed or removed by hand -- because the live page
    still shows them; the run after the push catches them. The one exception is games
    deleted on the site: the live page already hides those, so their covers go now."""
    keep = cover_refs(new_master) | cover_refs(g for g in live if g["name"] not in deleted)
    return sorted(k for k in remote if not k.startswith("added/") and k not in keep)


def live_deploy_done(sha):
    """True once GitHub Pages has finished deploying origin/main. Until then the live page may
    still be the build before it, whose covers the prune can't see -- so wait a run."""
    import urllib.request
    try:
        full = subprocess.run(["git", "-C", REPO, "rev-parse", "origin/main"], capture_output=True,
                              text=True).stdout.strip()
        req = urllib.request.Request(PAGES_RUNS % full, headers={"User-Agent": "the-backlog-sync"})
        runs = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("workflow_runs") or []
    except Exception as e:
        print("couldn't check the Pages deploy (%s)" % str(e)[:80])
        return False
    return any(r.get("status") == "completed" and r.get("conclusion") == "success" for r in runs)


def prune_covers(keys, s3, bucket, yes):
    if not keys:
        print("no unused covers in the bucket")
        return
    if len(keys) > PRUNE_CONFIRM_OVER and not yes:
        try:
            ok = input("delete %d unused covers from R2? [y/N] " % len(keys)).strip().lower() == "y"
        except EOFError:   # no terminal to ask on
            ok = False
        if not ok:
            print("left them alone (re-run with --yes to delete them)")
            return
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
    print("deleted %d unused covers from R2" % len(keys))


# ---- deciding what to do ----

def plan(master, deleted, added, live):
    """Sort every site change into what this run does about it."""
    in_master = {g["name"] for g in master}
    in_live = {g["name"] for g in live}
    p = {"adds": [], "handoffs": [], "waiting": [], "site_deletes": [], "catalog_deletes": [], "stale": []}
    for doc_id, r in sorted(added.items(), key=lambda kv: kv[1].get("n") or ""):
        name = r.get("n")
        if not name:
            continue
        if name in deleted:
            p["site_deletes"].append((doc_id, r))
        elif name not in in_master:
            p["adds"].append((doc_id, r))
        elif name in in_live:
            p["handoffs"].append((doc_id, r))
        else:
            p["waiting"].append((doc_id, r))
    doc_names = {r.get("n") for r in added.values()}
    p["catalog_deletes"] = sorted(n for n in deleted if n in in_master)
    p["stale"] = sorted(n for n in deleted if n not in in_master and n not in doc_names)
    return p


def report(p, sha, fetched):
    print("live site = origin/main %s%s\n" % (sha, "" if fetched else "  (couldn't fetch -- may be out of date)"))

    def section(title, rows, fmt):
        print("%s (%d)" % (title, len(rows)))
        for row in rows:
            print("   " + fmt(row))
        print()

    section("Add to the master file", p["adds"], lambda d: "+ %s  [%s]" % (
        d[1]["n"], "covers in added/, copied inside R2" if added_keys(d[1])
        else "older kind: covers built by refetch_covers.py"))
    section("Finish handoff: baked and live, so delete the Firestore record + added/ covers",
            p["handoffs"], lambda d: "~ " + d[1]["n"])
    section("Baked but not live yet (handoff finishes on a later run, after the push)",
            p["waiting"], lambda d: ". " + d[1]["n"])
    section("Finish site deletes: Firestore record + added/ covers", p["site_deletes"],
            lambda d: "x %s%s" % (d[1]["n"], "  (also in the master file)" if d[1]["n"] in p["catalog_deletes"] else ""))
    section("Remove from the master file (PERMANENT -- deleted on the site)", p["catalog_deletes"],
            lambda n: "- " + n)
    if p["stale"]:
        print("%d deleted name(s) with nothing left to remove (already gone)\n" % len(p["stale"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="show what a run would do, change nothing")
    ap.add_argument("--yes", action="store_true", help="don't ask before a large R2 delete")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    master = json.load(open(MASTER, encoding="utf-8"))
    ids = json.load(open(IDS_FILE, encoding="utf-8"))
    _db, user = connect()
    deleted, added = read_site(user)
    live, sha, fetched = live_master()
    p = plan(master, deleted, added, live)
    report(p, sha, fetched)
    try:
        s3, bucket = r2_client()
    except RuntimeError as e:
        sys.exit(str(e))
    deployed = live_deploy_done(sha)

    if args.dry_run:
        # Preview the prune against the master file as this run would leave it. Adds are
        # simulated without their HLTB lookups or R2 copies -- only their cover names matter.
        preview = [g for g in master if g["name"] not in set(p["catalog_deletes"])]
        for _doc_id, r in p["adds"]:
            rec = compact_to_master(r)
            wide, hero = added_key(r.get("cv")), added_key(r.get("hv"))
            if wide and hero:
                rec["cover"], rec["coverHero"] = wide[len("added/"):], hero[len("added/"):]
            elif r.get("cv"):
                rec["cover"] = r["cv"]
            preview.append(rec)
        if deployed:
            unused = unused_covers(preview, live, deleted, remote_keys(s3, bucket))
            print("Delete from R2, unused (%d)" % len(unused))
            for k in unused[:40]:
                print("   x " + k)
            if len(unused) > 40:
                print("   ... and %d more" % (len(unused) - 40))
        else:
            print("R2 prune: skipped -- the live deploy of %s isn't confirmed finished" % sha)
        print("\ndry run -- nothing changed")
        return

    # Master-file changes first; the Firestore and R2 deletes, which can't be undone, only
    # once those have gone through.
    backup = MASTER.replace(".json", ".backup_pre_sync.json")
    shutil.copy(MASTER, backup)
    print("backed up master -> %s" % os.path.basename(backup))
    legacy = add_games(p["adds"], master, ids, s3, bucket)
    remove_catalog_games(p["catalog_deletes"], master, ids)
    json.dump(master, open(MASTER, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(ids, open(IDS_FILE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    if legacy:
        build_legacy_covers(legacy)
        master = json.load(open(MASTER, encoding="utf-8"))   # refetch_covers.py rewrote it

    remove_site_records(p["handoffs"] + p["site_deletes"], added, user, s3, bucket)

    if deployed:
        prune_covers(unused_covers(master, live, deleted, remote_keys(s3, bucket)), s3, bucket, args.yes)
    else:
        print("R2 prune skipped: the live deploy of %s isn't confirmed finished -- the next run does it" % sha)

    subprocess.run([sys.executable, os.path.join(HERE, "build.py")], check=True)
    # Anything on disk the bucket lacks -- e.g. an embedded cover build.py just wrote out
    # because refetch_covers.py found nothing better -- or it'd be a broken image once pushed.
    remote = remote_keys(s3, bucket)
    missing = [(k, v) for k, v in local_files().items() if k not in remote]
    if missing:
        print("uploaded %d local cover(s) R2 didn't have" % upload(s3, bucket, missing, progress=False))

    print("\nDone. Review `git diff`, then commit + push.")
    if p["adds"]:
        print("After the push is live, run this again to finish handing the new games over.")


if __name__ == "__main__":
    main()
