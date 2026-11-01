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
echo "[1/3] remounting /data/adb/modules as writable via su"
$ADB shell su -c "mkdir -p $MOD && rm -rf $MOD/system"

echo "[2/3] pushing (large files go through /data/local/tmp)"
for f in $(find "$STAGE" -type f); do
  rel=${f#"$STAGE"/}
  $ADB push "$f" "/data/local/tmp/$(echo "$rel" | sed 's|/|_|g')"
  $ADB shell su -c "
    mkdir -p '$MOD/$(dirname "$rel")'
    cp -f '/data/local/tmp/$(echo "$rel" | sed 's|/|_|g')' '$MOD/$rel'
    rm -f '/data/local/tmp/$(echo "$rel" | sed 's|/|_|g')'
    chcon u:object_r:system_file:s0 '$MOD/$rel' 2>/dev/null || true
  "
done
$ADB shell su -c "ls -lR $MOD/system/app/webview | head -20"

echo "[3/3] reboot"
$ADB reboot
echo "After boot, verify with:"
echo "  adb shell dumpsys webviewupdate | head -20"
