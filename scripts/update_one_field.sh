#!/usr/bin/env bash
# Publishes the One Field submission package and updates the title:
#   1. converts the subtitled trailer to HLS and uploads it
#   2. uploads the poster, banner and English captions (.srt becomes .vtt)
#   3. runs the seed, which writes the details in scripts/wano_tv_seed.json
#
#   scripts/update_one_field.sh "<path to the submission .zip>"
#
# Run from the repo root. The film itself is already uploaded to
# wano-tv/one-field-v2/; everything here goes in the same folder.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <submission.zip>" >&2
  exit 1
fi

ZIP="$1"
FOLDER="one-field-v2"
PYTHON="venv/bin/python"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "==> Unpacking"
unzip -q "$ZIP" -d "$WORK/package"
find_file() {
  local found
  found=$(find "$WORK/package" -type f -name "$1" | head -1)
  if [ -z "$found" ]; then
    echo "Missing $1 in the zip" >&2
    exit 1
  fi
  echo "$found"
}
TRAILER=$(find_file "ONE_FIELD_Trailer_45s_Subtitled*.mp4")
POSTER=$(find_file "ONE_FIELD_Poster_9x16_1080.jpg")
BANNER=$(find_file "ONE_FIELD_Banner_16x9.jpg")
CAPTIONS=$(find_file "ONE_FIELD_Film_EN_Captions.srt")

echo "==> Converting the trailer to HLS"
scripts/mp4_to_hls.sh "$TRAILER" "$WORK/trailer-hls"

echo "==> Uploading the trailer"
"$PYTHON" -m scripts.upload_hls_to_spaces "$WORK/trailer-hls" "$FOLDER/trailer"

echo "==> Uploading artwork and captions"
"$PYTHON" -m scripts.upload_wano_tv_files "$FOLDER" "$POSTER" "$BANNER" "$CAPTIONS"

echo "==> Updating the title"
"$PYTHON" -m scripts.seed_wano_tv
