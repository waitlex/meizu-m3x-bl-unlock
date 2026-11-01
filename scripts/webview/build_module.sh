#!/usr/bin/env bash
# Assemble + install the systemless "webview106" Magisk module.
#
#   usage: build_module.sh SIGNED.apk BROMITE_APK_DIR... (see ARGS below)
#   usage: build_module.sh signed.apk bromite_arm64.apk bromite_arm.apk
#
# Layout produced on the device (this is exactly what is live on the M3X):
#   /data/adb/modules/webview106/module.prop
#   /data/adb/modules/webview106/system/app/webview/webview.apk
#   /data/adb/modules/webview106/system/app/webview/lib/arm64/*.so
#   /data/adb/modules/webview106/system/app/webview/lib/arm/*.so
#   /data/adb/modules/webview106/system/lib64/webview/*.so
#   /data/adb/modules/webview106/system/lib/webview/*.so
#
# Why two copies of the .so files: Meizu's patched framework looks the library
# up in /data/adb/modules/.../system/app/webview/lib/<isa> *and* the APK's
# nativeLibraryDirectories entry /system/app/webview/lib/<isa>; the
# /system/{lib,lib64}/webview copies are what the stock 44.x package used, and
# keeping them makes the mount byte-identical to the working install.
#
# Env overrides:
#   SERIAL   adb device serial (adb -s)
#   DRY      set to 1 to stop before touching the device
set -euo pipefail

APK=${1:?usage: build_module.sh signed.apk bromite_arm64.apk [bromite_arm.apk]}
A64=${2:?missing bromite arm64 apk}
A32=${3:-}

ID=webview106
STAGE=$(mktemp -d)/$ID
MOD=/data/adb/modules/$ID
ADB=$(command -v adb)
[ -n "${SERIAL:-}" ] && ADB="$ADB -s $SERIAL"

mkdir -p "$STAGE/system/app/webview/lib/arm64" \
         "$STAGE/system/app/webview/lib/arm" \
         "$STAGE/system/lib64/webview" \
         "$STAGE/system/lib/webview"

cat > "$STAGE/module.prop" <<'PROP'
id=webview106
name=WebView 106 (Bromite)
version=106.0.5249.163
versionCode=1060524
author=local
description=Replace stock Meizu WebView 44 with Bromite 106 via systemless mount
PROP

cp "$APK" "$STAGE/system/app/webview/webview.apk"

# Bromite ships the engine as lib/arm64-v8a/libwebviewchromium.so.  The ISA
# directory names Magisk/the linker expect come from VMRuntime.getTargetAbis(),
# i.e. "arm64" and "arm", so the entries are renamed while extracting.
extract() { # $1=source apk  $2=dest dir  $3...=prefixes
  local src=$1 dst=$2
  mkdir -p "$STAGE/tmpx"
  ( cd "$STAGE/tmpx" && unzip -q -o "$src" 'lib/*' )
  find "$STAGE/tmpx/lib" -name 'libwebviewchromium.so' -o -name 'libcrashpad_handler_trampoline.so' \
    | while read -r f; do cp -f "$f" "$dst/"; done
  rm -rf "$STAGE/tmpx"
}
extract "$A64" "$STAGE/system/app/webview/lib/arm64"
cp -f "$STAGE"/system/app/webview/lib/arm64/*.so "$STAGE/system/lib64/webview/"
if [ -n "$A32" ]; then
  extract "$A32" "$STAGE/system/app/webview/lib/arm"
  cp -f "$STAGE"/system/app/webview/lib/arm/*.so "$STAGE/system/lib/webview/"
fi

echo "staged at $STAGE"
find "$STAGE" -type f -exec ls -l {} \; | awk '{print $5, $9}'

[ "${DRY:-0}" = "1" ] && { echo "DRY=1, device untouched"; exit 0; }

$ADB wait-for-device
# `adb shell su -c "a && b"` hands `&& b` to the *device* shell, so b runs as
# uid 2000 and every write is EACCES -- the module then silently keeps whatever
# was in it before. All root writes go through one pushed script instead.
for i in $(seq 1 60); do
  b=$($ADB shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
  u=$($ADB shell "su -c 'id -u'" 2>/dev/null | tr -d '\r')
  [ "$b" = 1 ] && [ "$u" = 0 ] && break
  sleep 3
done
[ "$b" = 1 ] && [ "$u" = 0 ] || { echo "device not ready (boot=$b su=$u)"; exit 1; }

DEP=$(mktemp)
echo "mkdir -p $MOD && rm -rf $MOD/system $MOD/disable" > "$DEP"
echo "[1/3] pushing module files"
for f in $(find "$STAGE" -type f); do
  rel=${f#"$STAGE"/}; flat=$(echo "$rel" | sed 's|/|_|g')
  $ADB push "$f" "/data/local/tmp/$flat" >/dev/null 2>&1 \
    || $ADB push "$f" "/data/local/tmp/$flat" >/dev/null || { echo "push failed: $rel"; exit 1; }
  { echo "mkdir -p $MOD/$(dirname "$rel")"
    echo "cp -f /data/local/tmp/$flat $MOD/$rel"
    echo "rm -f /data/local/tmp/$flat"
    echo "chcon u:object_r:system_file:s0 $MOD/$rel 2>/dev/null"
  } >> "$DEP"
done
$ADB push "$DEP" /data/local/tmp/deploy.sh >/dev/null
$ADB shell "su -c 'sh /data/local/tmp/deploy.sh && rm -f /data/local/tmp/deploy.sh'" | tr -d '\r'
rm -f "$DEP"

# Android 6 has md5sum/sha1sum but *no* sha256sum, and an empty result looks
# exactly like a mismatch -- verify with md5 and abort if the module differs.
BAD=0
for f in $(find "$STAGE" -type f); do
  rel=${f#"$STAGE"/}
  [ "$(md5 -q "$f")" = "$($ADB shell "su -c 'md5sum $MOD/$rel 2>/dev/null'" | cut -c1-32 | tr -d '\r')" ] \
    || { echo "DEPLOY_MISMATCH $rel"; BAD=1; }
done
[ "$BAD" = 0 ] || { echo "module content does not match staging, aborting"; exit 1; }
echo "[2/3] deploy verified"

echo "[3/3] reboot"
$ADB reboot
echo "After boot, verify with:"
echo "  adb shell dumpsys package com.android.webview | grep versionName"
echo "  (dumpsys webviewupdate prints nothing on Flyme 5)"
