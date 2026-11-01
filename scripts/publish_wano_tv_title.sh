#!/usr/bin/env bash
# Publishes a Wano TV title's media to Spaces and updates the catalogue:
#   1. converts the film to HLS and uploads it to wano-tv/<folder>/
#   2. converts the trailer to HLS and uploads it to wano-tv/<folder>/trailer/
#   3. uploads the poster and the landscape image
#   4. adds captions to the film's stream (optional)
#   5. runs the seed, which writes the title's details from
#      scripts/wano_tv_seed.json (add the title there first)
#
#   scripts/publish_wano_tv_title.sh <folder> <film> <trailer> <poster> <landscape> [<captions.srt>]
#
# Run from the repo root. Files land under the same names, so the seed's URLs
# for this title should be:
#   video_url          .../wano-tv/<folder>/master.m3u8
#   trailer.video_url  .../wano-tv/<folder>/trailer/master.m3u8
#   poster/backdrop    .../wano-tv/<folder>/<poster file name>
#   trailer.thumbnail  .../wano-tv/<folder>/<landscape file name>
# Uploading over an existing folder can leave the CDN serving old segments for
# a long time; use a new folder name for a re-encode.
set -euo pipefail

if [ $# -lt 5 ] || [ $# -gt 6 ]; then
  echo "usage: $0 <folder> <film> <trailer> <poster> <landscape> [<captions.srt>]" >&2
  exit 1
fi

FOLDER="$1"
FILM="$2"
TRAILER="$3"
POSTER="$4"
LANDSCAPE="$5"
CAPTIONS="${6:-}"
PYTHON="venv/bin/python"

for f in "$FILM" "$TRAILER" "$POSTER" "$LANDSCAPE" ${CAPTIONS:+"$CAPTIONS"}; do
  if [ ! -f "$f" ]; then
    echo "Not found: $f" >&2
    exit 1
  fi
done

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "==> Converting the film to HLS (the long step)"
scripts/mp4_to_hls.sh "$FILM" "$WORK/film-hls"

echo "==> Uploading the film"
"$PYTHON" -m scripts.upload_hls_to_spaces "$WORK/film-hls" "$FOLDER"

echo "==> Converting the trailer to HLS"
scripts/mp4_to_hls.sh "$TRAILER" "$WORK/trailer-hls"

echo "==> Uploading the trailer"
"$PYTHON" -m scripts.upload_hls_to_spaces "$WORK/trailer-hls" "$FOLDER/trailer"

echo "==> Uploading artwork"
"$PYTHON" -m scripts.upload_wano_tv_files "$FOLDER" "$POSTER" "$LANDSCAPE"

if [ -n "$CAPTIONS" ]; then
  echo "==> Adding English subtitles to the film"
  "$PYTHON" -m scripts.add_hls_subtitles "$FOLDER" "$CAPTIONS" en English
fi

echo "==> Updating the catalogue"
"$PYTHON" -m scripts.seed_wano_tv
