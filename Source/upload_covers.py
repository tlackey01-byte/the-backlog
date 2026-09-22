"""Uploads docs/games/covers/ to the Cloudflare R2 bucket the site serves images from.

The repo doesn't carry cover images any more -- git would keep every version of every cover
forever, and a re-bake at a different size would add another ~120MB that can never be
reclaimed without rewriting history. R2 holds only the current set, and the worker's /img/
route serves it.

File names are content hashes, so an object that already exists in the bucket is byte-for-byte
what we would upload; those are skipped. That makes this safe and quick to re-run after a
partial cover refresh.

Setup (one time):
  1. Enable R2 in the Cloudflare dashboard, then: npx wrangler r2 bucket create the-backlog-covers
  2. Dashboard -> R2 -> API -> Create API token, Object Read & Write on that bucket
  3. Save Source/r2_credentials.json (gitignored):
       {"accountId": "...", "accessKeyId": "...", "secretAccessKey": "...",
        "bucket": "the-backlog-covers"}
  4. pip install boto3

  python Source/upload_covers.py            # upload what's missing
  python Source/upload_covers.py --prune    # also delete objects no local file matches
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
    """Every cover on disk as (r2 key, full path). Keys match what build.py puts in the page."""
    out = {}
    for root, _dirs, files in os.walk(COVERS_DIR):
        for f in files:
            full = os.path.join(root, f)
            key = os.path.relpath(full, COVERS_DIR).replace(os.sep, "/")
            out[key] = full
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", action="store_true", help="delete bucket objects with no local file")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    if not os.path.exists(CREDS):
        sys.exit("missing %s -- see the setup steps in this file's docstring" % CREDS)
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("boto3 is not installed: pip install boto3")

    c = json.load(open(CREDS, encoding="utf-8"))
    bucket = c.get("bucket", "the-backlog-covers")
    s3 = boto3.client(
        "s3",
        endpoint_url="https://%s.r2.cloudflarestorage.com" % c["accountId"],
        aws_access_key_id=c["accessKeyId"],
        aws_secret_access_key=c["secretAccessKey"],
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        region_name="auto",
    )

    files = local_files()
    print("%d cover files on disk (%.0fMB)" % (
        len(files), sum(os.path.getsize(p) for p in files.values()) / 1024 / 1024))

    remote = set()
    token = None
    while True:
        kw = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        remote.update(o["Key"] for o in page.get("Contents", []))
        token = page.get("NextContinuationToken")
        if not token:
            break
    print("%d objects already in the bucket" % len(remote))

    todo = [(k, v) for k, v in files.items() if k not in remote]
    if not todo:
        print("nothing to upload")
    else:
        done = [0]

        def put(item):
            key, path = item
            ctype = mimetypes.guess_type(key)[0] or "image/webp"
            with open(path, "rb") as f:
                s3.put_object(Bucket=bucket, Key=key, Body=f.read(), ContentType=ctype)
            done[0] += 1
            if done[0] % 100 == 0:
                print("  uploaded %d/%d" % (done[0], len(todo)), flush=True)

        print("uploading %d new objects..." % len(todo))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(put, todo))
        print("uploaded %d" % done[0])

    if args.prune:
        extra = sorted(remote - set(files))
        for i in range(0, len(extra), 1000):
            batch = extra[i:i + 1000]
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]})
        print("pruned %d objects with no local file" % len(extra))
    elif remote - set(files):
        print("%d bucket objects have no local file (run with --prune to remove them)"
              % len(remote - set(files)))


if __name__ == "__main__":
    main()
