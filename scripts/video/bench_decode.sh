#!/usr/bin/env bash
# Measure how the M3X actually decodes a clip: which codec is chosen,
# whether frames keep up, and how much CPU the decoder uses.
#
#   usage: bench_decode.sh /path/to/clip.mp4 [player-package]
#
# Default player is the stock Meizu video player; pass "org.videolan.vlc" to
# bench VLC (software decode) instead.
#
# NOTE on the measurement: `dumpsys SurfaceFlinger --latency <layer>` returns
# the last recorded frame timestamps.  If the player paused or the layer name
# is wrong you get *identical numbers for different files* -- that happened
# here twice with 4K clips.  So this script also grabs a screenshot and lets
# you confirm the clip is really playing, and it records the decoder chosen
# from logcat rather than trusting the latency table alone.
set -euo pipefail

CLIP=${1:?usage: bench_decode.sh clip.mp4 [player-package]}
PKG=${2:-}
ADB=${ADB:-adb}
[ -n "${SERIAL:-}" ] && ADB="$ADB -s $SERIAL"
S() { $ADB shell su -c "$1" 2>&1; }

DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$CLIP" 2>/dev/null || echo 10)
DUR=${DUR%.*}; [ "${DUR:-0}" -gt 0 ] 2>/dev/null || DUR=10
BASE=$(basename "$CLIP")

$ADB shell am force-stop com.android.shell >/dev/null 2>&1 || true
$ADB logcat -c
echo "[1/5] push $BASE"
$ADB push "$CLIP" "/sdcard/Download/$BASE" >/dev/null

echo "[2/5] start playback"
if [ -n "$PKG" ]; then
  $ADB shell monkey -p "$PKG" 1 >/dev/null
  sleep 4
  $ADB shell "am start -a android.intent.action.VIEW \
    -d 'file:///sdcard/Download/$BASE' -t video/*" >/dev/null
else
  $ADB shell "am start -a android.intent.action.VIEW \
    -d 'file:///sdcard/Download/$BASE' -t video/*" >/dev/null
fi
sleep 6

echo "[3/5] decoder actually selected (logcat)"
$ADB logcat -d 2>/dev/null \
  | grep -Eio "OMX\.[A-Za-z0-9._]+|c2\.[a-z0-9.]+" \
  | sort -u | sed 's/^/      /' || echo "      (nothing matched -- check logcat buffer)"

echo "[4/5] SurfaceFlinger frame intervals (first layer that has data)"
S "dumpsys SurfaceFlinger --latency-list" 2>/dev/null | head -5 | sed 's/^/      /'
for layer in "VideoSurface" "SurfaceView" $(S "dumpsys SurfaceFlinger" \
        | grep -oE "SurfaceView[^ ]*" | head -3); do
  lat=$(S "dumpsys SurfaceFlinger --latency '$layer'" 2>/dev/null | sed -n '4,60p')
  if [ -n "$lat" ]; then
    echo "      layer=$layer"
    echo "$lat" | awk 'NF==3 && $2>0 { if (p) { d=$2-p; sum+=d; n++;
        if (d>mx) mx=d } p=$2 } END {
        if (n) printf("      frames=%d  avg=%.1fms  max=%.1fms  (33.3ms = 30fps)\n", n+1, sum/n, mx);
        else print "      no timestamps" }'
    break
  fi
done

echo "[5/5] decoder process CPU (3 samples)"
PAT="media|codec"
[ -n "$PKG" ] && PAT="$PAT|$(echo "$PKG" | cut -d. -f1)"
for i in 1 2 3; do
  S "top -n 1 -m 12 -s cpu" 2>/dev/null \
    | grep -Ei "$PAT" | head -4 | sed 's/^/      /'
  sleep 2
done
$ADB shell screencap -p /sdcard/Download/bench_$BASE.png 2>/dev/null \
  && $ADB pull "/sdcard/Download/bench_$BASE.png" "/tmp/bench_${BASE}.png" >/dev/null \
  && echo "      screenshot: /tmp/bench_${BASE}.png  (verify the clip is really playing)"

echo
echo "Cleanup: $ADB shell rm /sdcard/Download/$BASE"
