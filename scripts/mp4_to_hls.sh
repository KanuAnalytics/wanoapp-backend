#!/usr/bin/env bash
# Converts an MP4 into multi-quality HLS for Wano TV: one folder per quality
# plus a master.m3u8 that lists them, so the player can switch quality.
#
#   scripts/mp4_to_hls.sh <input.mp4> <output-folder>
#
# Qualities are 2160p (4K), 1080p, 720p and 360p; ones taller than the source
# are skipped, so nothing is upscaled. Set PRESET to trade speed for
# quality (x264 presets: veryfast, fast, medium, slow); default is fast.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <input.mp4> <output-folder>" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "ffmpeg isn't installed. Install it with: brew install ffmpeg" >&2
  exit 1
fi

INPUT="$1"
OUT="$2"
PRESET="${PRESET:-fast}"
SEGMENT_SECONDS=6

# height:video bitrate:max bitrate:buffer
LADDER=(
  "2160:16000k:17120k:24000k"
  "1080:5000k:5350k:7500k"
  "720:2800k:2996k:4200k"
  "360:800k:856k:1200k"
)

# Newer ffprobe can list the same stream more than once (e.g. inside a stream
# group), so keep only the first number.
SOURCE_HEIGHT=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$INPUT" | grep -Eo '[0-9]+' | head -1)
if [ -z "$SOURCE_HEIGHT" ]; then
  echo "Couldn't read the video height of $INPUT" >&2
  exit 1
fi
# Same quirk: the audio index can come after a blank line, so look for any number.
HAS_AUDIO=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$INPUT" | grep -Eo '[0-9]+' | head -1 || true)

RUNGS=()
for rung in "${LADDER[@]}"; do
  height="${rung%%:*}"
  if [ "$height" -le "$SOURCE_HEIGHT" ]; then RUNGS+=("$rung"); fi
done
# A source smaller than 360p still gets one quality at its own size.
if [ ${#RUNGS[@]} -eq 0 ]; then RUNGS=("$SOURCE_HEIGHT:800k:856k:1200k"); fi

COUNT=${#RUNGS[@]}
FILTER="[0:v]split=$COUNT"
for i in $(seq 0 $((COUNT - 1))); do FILTER+="[v$i]"; done
FILTER+=";"

ARGS=()
STREAM_MAP=""
for i in $(seq 0 $((COUNT - 1))); do
  IFS=: read -r height bitrate maxrate bufsize <<< "${RUNGS[$i]}"
  FILTER+="[v$i]scale=-2:$height[v${i}out];"
  ARGS+=(-map "[v${i}out]"
    -c:v:$i libx264 -preset "$PRESET" -profile:v:$i high
    -b:v:$i "$bitrate" -maxrate:v:$i "$maxrate" -bufsize:v:$i "$bufsize")
  if [ -n "$HAS_AUDIO" ]; then
    ARGS+=(-map a:0 -c:a:$i aac -b:a:$i 128k -ac 2)
    STREAM_MAP+="v:$i,a:$i,name:${height}p "
  else
    STREAM_MAP+="v:$i,name:${height}p "
  fi
done

mkdir -p "$OUT"

ffmpeg -hide_banner -y -i "$INPUT" \
  -filter_complex "${FILTER%;}" \
  "${ARGS[@]}" \
  -pix_fmt yuv420p \
  -force_key_frames "expr:gte(t,n_forced*$SEGMENT_SECONDS)" \
  -sc_threshold 0 \
  -f hls \
  -hls_time "$SEGMENT_SECONDS" \
  -hls_playlist_type vod \
  -hls_flags independent_segments \
  -hls_segment_filename "$OUT/%v/segment_%04d.ts" \
  -master_pl_name master.m3u8 \
  -var_stream_map "${STREAM_MAP% }" \
  "$OUT/%v/index.m3u8"

echo "Done: $OUT/master.m3u8"
