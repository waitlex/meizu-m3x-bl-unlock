#!/usr/bin/env bash
# Re-sign a patched WebView APK with a self-signed v1 (JAR) signature.
#
# Android 6 only verifies the v1 signature for /system apps and the platform
# rejects SHA-1 here, so both digests must be SHA-256.
#
#   usage: sign_apk.sh IN.apk OUT.apk
#
# Env overrides:
#   KS      keystore path           (default: ./webview.keystore, never committed)
#   ALIAS   key alias               (default: webview)
#   KSPASS  keystore/key password   (default: webview)
set -euo pipefail

IN=${1:?usage: sign_apk.sh IN.apk OUT.apk}
OUT=${2:?usage: sign_apk.sh IN.apk OUT.apk}
KS=${KS:-./webview.keystore}
ALIAS=${ALIAS:-webview}
KSPASS=${KSPASS:-webview}

PY=${PYTHON:-python3}
JARSIGNER=${JARSIGNER:-$(command -v jarsigner || echo /tmp/jdk/Contents/Home/bin/jarsigner)}
KEYTOOL=${KEYTOOL:-$(command -v keytool || echo "$(dirname "$JARSIGNER")/keytool")}

[ -f "$IN" ] || { echo "no such input: $IN"; exit 1; }

if [ ! -f "$KS" ]; then
  echo "[1/3] generating self-signed keystore $KS (alias=$ALIAS)"
  "$KEYTOOL" -genkeypair -v -keystore "$KS" -alias "$ALIAS" \
    -keyalg RSA -keysize 2048 -validity 10000 -storepass "$KSPASS" \
    -dname "CN=WebView Patch, OU=dev, O=local, C=CN"
else
  echo "[1/3] reusing keystore $KS"
fi

cp -f "$IN" "$OUT"
echo "[2/3] signing -> $OUT"
"$JARSIGNER" -keystore "$KS" -storepass "$KSPASS" -keypass "$KSPASS" \
  -digestalg SHA-256 -sigalg SHA256withRSA \
  -sigfile WEBVIEW "$OUT" "$ALIAS"

echo "[3/3] verify"
"$JARSIGNER" -verify -keystore "$KS" -storepass "$KSPASS" "$OUT" >/dev/null
"$PY" - "$OUT" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
names = z.namelist()
assert any(n.startswith("META-INF/") and n.endswith(".RSA") for n in names), "no signature block"
assert "resources.arsc" in names, "resources.arsc missing"
print("entries:", len(names))
print("OK: v1 signature present")
PY
