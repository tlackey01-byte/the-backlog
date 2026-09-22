#!/bin/sh
# Uploads docs/games/covers/ to the R2 bucket using wrangler's existing OAuth login.
#
# Source/upload_covers.py is much faster (S3 API, ~2 minutes vs ~20) but needs an R2 API
# token created in the dashboard. This one needs nothing beyond `wrangler login`, at the
# cost of spawning a node process per file, so it's the fallback for a one-off bulk load.
#
#   sh Source/upload_covers_wrangler.sh [parallelism]
#
# Object keys mirror the local layout ("<hash>.webp", "hero/<hash>.webp"), which is what
# build.py writes into the page and what the worker's /img/ route expects.

set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
COVERS="$ROOT/docs/games/covers"
BUCKET=the-backlog-covers
PAR=${1:-14}

cd "$COVERS"
find . -type f \( -name '*.webp' -o -name '*.jpg' -o -name '*.png' \) \
  | sed 's|^\./||' > /tmp/r2_keys.txt
TOTAL=$(wc -l < /tmp/r2_keys.txt)
echo "uploading $TOTAL objects to $BUCKET with parallelism $PAR"

xargs -P "$PAR" -I{} sh -c '
  key="{}"
  case "$key" in
    *.jpg) ct=image/jpeg ;;
    *.png) ct=image/png ;;
    *)     ct=image/webp ;;
  esac
  npx wrangler r2 object put "'"$BUCKET"'/$key" --file="$key" --content-type="$ct" --remote >/dev/null 2>&1 \
    || echo "FAILED $key"
' < /tmp/r2_keys.txt

echo "done"
