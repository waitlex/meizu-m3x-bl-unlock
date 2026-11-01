#!/usr/bin/env bash
# Generate the decode-matrix samples used in docs/06.
# Needs a desktop ffmpeg (homebrew's works); the files are ~8 s each.
#
#   usage: make_samples.sh [outdir]     (default: /tmp/decode-samples)
set -euo pipefail

OUT=${1:-/tmp/decode-samples}
mkdir -p "$OUT"
FF=${FFMPEG:-ffmpeg}
command -v "$FF" >/dev/null || { echo "ffmpeg not found (set FFMPEG=...)"; exit 1; }

# testsrc2 gives motion + fine detail, so a dropped-frame run is actually visible.
gen() { # $1=name  $2...=codec args
  local name=$1; shift
  [ -f "$OUT/$name.mp4" ] && { echo "skip $name (exists)"; return; }
  echo "  -> $name"
  "$FF" -y -loglevel error -f lavfi -i "testsrc2=size=$RES:rate=$RATE:duration=$DUR" \
    -c:v "$@" -pix_fmt "$PIX" -preset ultrafast -b:v 6M -movflags +faststart \
    "$OUT/$name.mp4"
}

echo "[1/6] AVC 1080p30 (baseline, 8-bit)"
RES=1920x1080 RATE=30 DUR=10 PIX=yuv420p
gen avc1080 libx264 -profile:v baseline

echo "[2/6] AVC 1080p60"
RES=1920x1080 RATE=60 DUR=10 PIX=yuv420p
gen avc1080p60 libx264 -profile:v baseline

echo "[3/6] HEVC 1080p30 (Main, 8-bit)"
RES=1920x1080 RATE=30 DUR=10 PIX=yuv420p
gen hevc1080 libx265 -x265-params "profile=main"

echo "[4/6] HEVC 1080p60 (Main, 8-bit)"
RES=1920x1080 RATE=60 DUR=10 PIX=yuv420p
gen hevc1080p60 libx265 -x265-params "profile=main"

echo "[5/6] HEVC 4K30 (Main, 8-bit)"
RES=3840x2160 RATE=30 DUR=8 PIX=yuv420p
gen hevc4k libx265 -x265-params "profile=main"

echo "[6/6] HEVC 1080p30 Main10 (10-bit)  <-- the one the SoC cannot decode"
RES=1920x1080 RATE=30 DUR=10 PIX=p010le
gen hevc10_1080 libx265 -x265-params "profile=main10"

for f in "$OUT"/*.mp4; do
  printf '%-16s %10s B  ' "$(basename "$f")" "$(wc -c <"$f" | tr -d ' ')"
  ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,profile,width,height,r_frame_rate,nb_frames \
    -of csv=p=0 "$f" 2>/dev/null || echo "(ffprobe missing)"
done
