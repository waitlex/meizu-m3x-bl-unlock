#!/usr/bin/env bash
# One Bromite version round: fetch a SystemWebView build, install it as the
# com.android.webview provider through the systemless module, reboot, and gate
# on "32-bit apps still start".
#
#   usage: bromite_round.sh <bromite-tag>        # e.g. 98.0.4758.116
#   env:   W=/tmp/wvb   cache/work dir (APKs, patched APKs, logs)
#
# The module is left ENABLED on PASS and DISABLED on FAIL, so the phone is
# always usable when a round ends.  Verdicts on the M3X (Flyme 5.2.8.3A):
#
#   105.0.5195.147  FAIL  arm32 SIGBUS/BUS_ADRALN in libwebviewchromium.so
#   101.0.4951.69   FAIL  same fault, pc 022ff156
#    99.0.4844.77   FAIL  same fault, pc 022b9a62
#    98.0.4758.116  PASS  gallery + Via render, no tombstone/ANR   <- ceiling
#    90.0.4430.204  PASS
#
# Four gates, all required -- each one exists because a round failed silently
# without it:
#   deploy  md5 of every module file must equal the staged copy
#   GATEVER dumpsys versionName must equal the tag (else the round measured the
#           previous provider, which is what produced a fake PASS once)
#   GATE32  32-bit app starts, no "Fatal signal", no new tombstone/ANR
#   GATE64  64-bit Via starts and survives a page load
set -uo pipefail

TAG=${1:?usage: bromite_round.sh <bromite-tag>}
W=${W:-/tmp/wvb}
SC=$(cd "$(dirname "$0")" && pwd)
export PYTHONPATH=$SC:$W:${PYTHONPATH:-}
MIR=https://ghfast.top
REL=https://github.com/bromite/bromite/releases/download
D=$W/$TAG
MOD=/data/adb/modules/webview106
KS=$W/webview.keystore
export KS
log() { echo "[$(date +%H:%M:%S)] $*"; }
die()   { echo "RESULT tag=$TAG verdict=ERROR reason=$1"; exit 1; }

mkdir -p "$D"; cd "$D" || die cd

# ---- 1. fetch + integrity -------------------------------------------------
if [ ! -s raw_arm64.apk ]; then
  log "download arm64_SystemWebView.apk ($TAG)"
  curl -fL --retry 3 -C - -o raw_arm64.apk "$MIR/$REL/$TAG/arm64_SystemWebView.apk" || die dl_arm64
fi
curl -fL --retry 2 -s -o sha256.txt "$MIR/$REL/$TAG/brm_$TAG.sha256.txt" 2>/dev/null
want=$(grep 'arm64_SystemWebView.apk' sha256.txt 2>/dev/null | grep -oE '[0-9a-f]{64}' | head -1)
got=$(shasum -a 256 raw_arm64.apk | cut -d' ' -f1)
if [ -n "$want" ]; then
  [ "$want" = "$got" ] || die "sha256_mismatch want=$want got=$got"
  log "sha256 OK $got"
else
  log "WARN no sha256 entry, size only: $(stat -f %z raw_arm64.apk)"
fi

# ---- 2. what are we patching? --------------------------------------------
python3 "$W/pkgname.py" raw_arm64.apk | tee pkg.txt || die inspect
grep -q 'SO lib/armeabi-v7a' pkg.txt || NEED_ARM=1
log "NEED_ARM=${NEED_ARM:-0}"
if [ "${NEED_ARM:-0}" = 1 ] && [ ! -s raw_arm.apk ]; then
  log "download arm_SystemWebView.apk too"
  curl -fL --retry 3 -C - -o raw_arm.apk "$MIR/$REL/$TAG/arm_SystemWebView.apk" || die dl_arm
fi

# ---- 3. package rename + arsc + resign -----------------------------------
# The manifest `package` attribute is what Android 6 registers the provider
# under, so it must become com.android.webview. Read it from the manifest
# itself -- the string-pool heuristic in pkgname.py grabs e.g.
# "com.android.webview.SHOW_DEV_UI" instead, and a wrong OLD makes axml_patch a
# silent no-op (APK stays org.bromite.webview → never becomes the provider).
OLD=$(python3 "$W/manifest_info.py" raw_arm64.apk | sed -n 's/^PKG //p')
[ -n "$OLD" ] || die no_pkg
log "manifest package = $OLD"
# Always route step1 through axml_patch even when no rename is needed: it is
# what drops the vendor's own META-INF/*.SF|RSA. Leaving a second signer in the
# APK makes Android 6 Signature.fromJar throw, and PMS then skips the package
# completely (dumpsys com.android.webview is empty) -- seen with Bromite 90/87,
# whose package name is already com.android.webview.
python3 "$SC/axml_patch.py" raw_arm64.apk step1.apk "$OLD" com.android.webview || die axml
chk=$(python3 "$W/manifest_info.py" step1.apk | sed -n 's/^PKG //p')
[ "$chk" = com.android.webview ] || die "axml_noop pkg=$chk"
unzip -l step1.apk | grep -qE 'META-INF/.*\.(SF|RSA|DSA|EC)' && die stale_signer_present
log "step1 pkg=$chk, vendor signatures stripped"
python3 "$SC/patch_arsc.py" step1.apk step2.apk || die arsc
bash "$SC/sign_apk.sh" step2.apk signed.apk >/dev/null || die sign
log "signed: $(stat -f %z signed.apk) B"

# ---- 4. stage the module --------------------------------------------------
STAGE=$D/module; rm -rf "$STAGE"
mkdir -p "$STAGE/system/app/webview/lib/arm64" "$STAGE/system/app/webview/lib/arm" \
         "$STAGE/system/lib64/webview" "$STAGE/system/lib/webview"
VC=$(python3 -c "p='$TAG'.split('.');print(int(p[0])*10000000+int(p[1])*1000+int(p[2]))")
cat > "$STAGE/module.prop" <<PROP
id=webview106
name=WebView $TAG (Bromite)
version=$TAG
versionCode=$VC
author=local
description=bisect candidate $TAG
PROP
cp signed.apk "$STAGE/system/app/webview/webview.apk"
rm -rf "$D/x"; mkdir -p "$D/x"; (cd "$D/x" && unzip -q -o ../raw_arm64.apk 'lib/*')
[ "${NEED_ARM:-0}" = 1 ] && (cd "$D/x" && unzip -q -o ../raw_arm.apk 'lib/*')
find "$D/x/lib" -name '*.so' | while read -r f; do
  case "$f" in *arm64-v8a*) cp -f "$f" "$STAGE/system/app/webview/lib/arm64/";;
                      *)   cp -f "$f" "$STAGE/system/app/webview/lib/arm/";;
  esac
done
cp -f "$STAGE"/system/app/webview/lib/arm64/*.so "$STAGE/system/lib64/webview/"
cp -f "$STAGE"/system/app/webview/lib/arm/*.so   "$STAGE/system/lib/webview/"
log "staged:"; find "$STAGE" -type f -exec stat -f '%z %N' {} \; | sed "s|$STAGE|.|;s|$D/||"
[ -s "$STAGE/system/app/webview/lib/arm/libwebviewchromium.so" ] || die no_arm_lib

# ---- 5. deploy + reboot ---------------------------------------------------
# Quoting rules learned the hard way:
#   * `adb shell su -c "a && b"` hands `&& b` to the *device* shell → b runs as
#     uid 2000 → every write is EACCES.
#   * a host `su -c '...'` payload cannot itself contain single quotes.
# So the writes go into one remote script, pushed flat and run by root sh.
rsu() { adb shell "su -c '$*'"; }
# `adb wait-for-device` returns as soon as adbd is *visible*, which during the
# previous round's reboot is early enough that pushes die with
# "failed to read copy response: EOF". Wait for a rooted, fully booted device.
wait_ready() {
  for i in $(seq 1 60); do
    b=$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    u=$(adb shell "su -c 'id -u'" 2>/dev/null | tr -d '\r')
    [ "$b" = 1 ] && [ "$u" = 0 ] && { log "device ready (boot_completed, uid 0) after ${i}x3s"; return 0; }
    sleep 3
  done
  die "device not ready (boot=$b su=$u)"
}
wait_ready || exit 1

DEP=$D/deploy.sh; : > "$DEP"
echo "mkdir -p $MOD && rm -rf $MOD/system $MOD/disable" >> "$DEP"
for f in $(find "$STAGE" -type f); do
  rel=${f#"$STAGE"/}; flat=$(echo "$rel" | sed 's|/|_|g')
  adb push "$f" "/data/local/tmp/$flat" >/dev/null 2>&1 || adb push "$f" "/data/local/tmp/$flat" >/dev/null || die push
  { echo "mkdir -p $MOD/$(dirname "$rel")"
    echo "cp -f /data/local/tmp/$flat $MOD/$rel"
    echo "rm -f /data/local/tmp/$flat"
    echo "chcon u:object_r:system_file:s0 $MOD/$rel 2>/dev/null"
  } >> "$DEP"
done
adb push "$DEP" /data/local/tmp/deploy.sh >/dev/null || die push_dep
rsu "sh /data/local/tmp/deploy.sh && rm -f /data/local/tmp/deploy.sh" | tr -d '\r' | tee "$D/deploy.out"
grep -qiE 'permission denied|no space|read-only' "$D/deploy.out" && die deploy_errors

# Hard gate: the module on the device must be byte-identical to what we staged,
# otherwise the round would silently test the previous provider.
# Android 6 ships md5sum/sha1sum but *no* sha256sum (empty output looks like a
# mismatch, so a failed verify must never be read as "the file differs").
BAD=0
for f in $(find "$STAGE" -type f); do
  rel=${f#"$STAGE"/}
  h1=$(md5 -q "$f")
  h2=$(adb shell "su -c 'md5sum $MOD/$rel 2>/dev/null'" | cut -c1-32 | tr -d '\r')
  [ "$h1" = "$h2" ] || { echo "DEPLOY_MISMATCH $rel host=$h1 dev=$h2"; BAD=1; }
done
[ "$BAD" = 0 ] || die deploy_verify
log "deploy verified (all module files match staged hashes)"
adb reboot
sleep 30
wait_ready || exit 1
log "settling"
sleep 20

# ---- 6. gates -------------------------------------------------------------
# Hard gate: if the provider is not the candidate we just staged, every later
# measurement would describe some *other* engine, so the round has no meaning.
revert() { adb shell "su -c 'touch $MOD/disable'"; adb reboot; log "module disabled, stock provider restored"; }
VER=$(adb shell "dumpsys package com.android.webview | grep -m1 versionName" | tr -d '\r')
log "provider $VER"
if ! echo "$VER" | grep -q "$TAG"; then
  revert
  echo "RESULT tag=$TAG verdict=INVALID reason=provider_mismatch got=$VER"
  exit 1
fi
echo "GATEVER PASS $VER"

TOMB0=$(adb shell "su -c 'ls /data/system/dropbox'" | tr -d '\r' | grep SYSTEM_TOMBSTONE | sort)
T0=$(echo "$TOMB0" | grep -c .)
dump_new() { # $1 = label; print the lines that identify each new crash site
  LB=$(adb shell "su -c 'ls /data/system/dropbox'" | tr -d '\r' | grep SYSTEM_TOMBSTONE | sort)
  for t in $(comm -13 <(echo "$TOMB0") <(echo "$LB") | grep -oE '[0-9]{13}' | head -3); do
    adb exec-out "su -c 'cat /data/system/dropbox/SYSTEM_TOMBSTONE@$t.txt.gz'" | gunzip 2>/dev/null \
      | grep -E "ABI:|>>> |^signal |libwebviewchromium" | head -6 | sed "s/^/EV$1 /"
  done
  TOMB0=$LB
}
ANR0=$(adb shell "su -c 'ls /data/system/dropbox | grep -c data_app_anr'" | tr -d '\r')
OUT=$(adb shell "su -c 'logcat -c; monkey -p com.meizu.media.gallery -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; sleep 12; echo GPID=\$(pgrep -f com.meizu.media.gallery | head -1); echo FATAL=\$(logcat -d -v brief | grep -c \"Fatal signal\"); echo TOMB=\$(ls /data/system/dropbox | grep -c SYSTEM_TOMBSTONE)'")
echo "$OUT"
GPID=$(echo "$OUT" | sed -n 's/GPID=//p' | tr -d '\r'); GF=$(echo "$OUT" | sed -n 's/FATAL=//p' | tr -d '\r'); T1=$(echo "$OUT" | sed -n 's/TOMB=//p' | tr -d '\r')
G32=FAIL
ANR1=$(adb shell "su -c 'ls /data/system/dropbox | grep -c data_app_anr'" | tr -d '\r')
if [ -n "$GPID" ] && [ "$GF" = 0 ] && [ "$T1" = "$T0" ] && [ "$ANR1" = "$ANR0" ]; then G32=PASS; fi
log "GATE32 (32-bit gallery) = $G32"
[ "$G32" = PASS ] || dump_new 32

G64=SKIP
if [ "$G32" = PASS ]; then
  OUT=$(adb shell "su -c 'am force-stop mark.via; sleep 1; logcat -c; monkey -p mark.via -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; sleep 8; am start -a android.intent.action.VIEW -d \"https://www.baidu.com/s?wd=localsend\" -p mark.via >/dev/null 2>&1; sleep 18; echo VPID=\$(pgrep -f mark.via | head -1); echo VFATAL=\$(logcat -d -v brief | grep -c \"Fatal signal\"); echo VTOMB=\$(ls /data/system/dropbox | grep -c SYSTEM_TOMBSTONE)'")
  echo "$OUT"
  VP=$(echo "$OUT" | sed -n 's/VPID=//p' | tr -d '\r'); VF=$(echo "$OUT" | sed -n 's/VFATAL=//p' | tr -d '\r'); T2=$(echo "$OUT" | sed -n 's/VTOMB=//p' | tr -d '\r')
  # NOTE: `am start -a VIEW -p mark.via` makes Via's .Trampoline ANR on its own
  # ("no window has focus"), so an ANR here must be read together with the
  # main-thread trace; the engine itself is exercised by the rendered page.
  ANR2=$(adb shell "su -c 'ls /data/system/dropbox | grep -c data_app_anr'" | tr -d '\r')
  [ -n "$VP" ] && [ "$VF" = 0 ] && [ "$T2" = "$T1" ] && G64=PASS || G64=FAIL
  echo "ANR_COUNT anr0=$ANR1 anr2=$ANR2"
  log "GATE64 (Via arm64) = $G64"
  [ "$G64" = PASS ] || dump_new 64
fi

if [ "$G32" != PASS ] || [ "$G64" = FAIL ]; then
  log "reverting module (leave stock provider)"
  revert
else
  log "candidate looks usable, module left enabled"
fi
echo "RESULT tag=$TAG gate32=$G32 gate64=$G64 provider=$VER"
