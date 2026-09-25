"""Uploads docs/games/covers/ to the Cloudflare R2 bucket the site serves images from.

The repo doesn't carry cover images any more -- git would keep every version of every cover
forever, and a re-bake at a different size would add another ~120MB that can never be
reclaimed without rewriting history. R2 holds only the current set, and the worker's /img/
route serves it.

File names are content hashes, so an object that already exists in the bucket is byte-for-byte
what we would upload; those are skipped. That makes this safe and quick to re-run after a
partial cover refresh.

refetch_covers.py uploads each image as it builds it (through upload() below), so this is
mostly a repair tool now -- for when an upload failed or ran offline. It never deletes
anything: the bucket's added/ folder holds images the site uploads for games added there,
which have no local file by design, so "no local file" doesn't mean "unused". Removing
unused images is sync_site.py's job, which checks what the master file and the live site
actually use.

Setup (one time):
  1. Enable R2 in the Cloudflare dashboard, then: npx wrangler r2 bucket create the-backlog-covers
  2. Dashboard -> R2 -> API -> Create API token, Object Read & Write on that bucket
  3. Save Source/r2_credentials.json (gitignored):
       {"accountId": "...", "accessKeyId": "...", "secretAccessKey": "...",
        "bucket": "the-backlog-covers"}
  4. pip install boto3

  python Source/upload_covers.py            # upload what's missing
"""

import argparse
import json
import mimetypes
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
COVERS_DIR = os.path.join(os.path.dirname(HERE), "docs", "games", "covers")
CREDS = os.path.join(HERE, "r2_credentials.json")


def local_files():
    """Every cover on disk as (r2 key, full path). Keys match what build.py puts in the page.
    Images only: a wrangler run from inside this folder once left its .wrangler/ cache here,
    and it went up to the bucket along with the covers."""
    out = {}
    for root, _dirs, files in os.walk(COVERS_DIR):
        for f in files:
            if not f.lower().endswith((".webp", ".jpg", ".png")):
                continue
            full = os.path.join(root, f)
            key = os.path.relpath(full, COVERS_DIR).replace(os.sep, "/")
            out[key] = full
    return out


def r2_client():
    """(boto3 S3 client for the bucket, bucket name). Raises RuntimeError when the credentials
    file or boto3 is missing, so a caller that can carry on without R2 (refetch_covers.py)
    can say so and keep going instead of exiting."""
    if not os.path.exists(CREDS):
        raise RuntimeError("missing %s -- see the setup steps in upload_covers.py" % CREDS)
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise RuntimeError("boto3 is not installed: pip install boto3")
    c = json.load(open(CREDS, encoding="utf-8"))
    s3 = boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % c["accountId"],
        aws_access_key_id=c["accessKeyId"],
        aws_secret_access_key=c["secretAccessKey"],
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        region_name="auto",
    )
    return s3, c.get("bucket", "the-backlog-covers")


def remote_keys(s3, bucket, prefix=""):
    """Every key in the bucket (under prefix), 1000 per listing call."""
    keys, token = set(), None
    while True:
        kw = {"Bucket": bucket, "MaxKeys": 1000, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys.update(o["Key"] for o in page.get("Contents", []))
        token = page.get("NextContinuationToken")
        if not token:
            return keys


def upload(s3, bucket, items, workers=16, progress=True):
    """Put each (key, local path) into the bucket. Returns how many went up."""
    done = [0]

    def put(item):
        key, path = item
        ctype = mimetypes.guess_type(key)[0] or "image/webp"
        with open(path, "rb") as f:
            s3.put_object(Bucket=bucket, Key=key, Body=f.read(), ContentType=ctype)
        done[0] += 1
        if progress and done[0] % 100 == 0:
            print("  uploaded %d/%d" % (done[0], len(items)), flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(put, items))
    return done[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    try:
        s3, bucket = r2_client()
    except RuntimeError as e:
        sys.exit(str(e))

    files = local_files()
    print("%d cover files on disk (%.0fMB)" % (
        len(files), sum(os.path.getsize(p) for p in files.values()) / 1024 / 1024))

    remote = remote_keys(s3, bucket)
    print("%d objects already in the bucket" % len(remote))

    todo = [(k, v) for k, v in files.items() if k not in remote]
    if not todo:
        print("nothing to upload")
    else:
        print("uploading %d new objects..." % len(todo))
        print("uploaded %d" % upload(s3, bucket, todo, args.workers))

    # Reported, never deleted (see the docstring). added/ is left out of the count: the site's
    # uploads are supposed to have no local file.
    stray = [k for k in remote - set(files) if not k.startswith("added/")]
    if stray:
        print("%d bucket objects have no local file -- left alone; removing unused images is "
              "sync_site.py's job" % len(stray))


if __name__ == "__main__":
    main()
