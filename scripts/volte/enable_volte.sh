#!/usr/bin/env bash
# Enable MTK VoLTE + IMS短信 on a Meizu/MTK phone that has data but no call/SMS.
#
# Run over adb with root.  Idempotent: it prints the current value before
# touching anything and re-reads everything at the end.
#
#   usage: enable_volte.sh            # apply
#   usage: enable_volte.sh --show     # only dump current state
#   usage: enable_volte.sh --revert   # put the props back to 0 / remove the APN we added
set -euo pipefail

ADB=${ADB:-adb}
[ -n "${SERIAL:-}" ] && ADB="$ADB -s $SERIAL"
MODE=${1:-apply}

S() { $ADB shell su -c "$1" 2>&1; }
getprop() { $ADB shell su -c "getprop $1" 2>/dev/null | tr -d '\r'; }

# --- SIM identity ---------------------------------------------------------
OP=$(getprop gsm.sim.operator.numeric)
[ -n "$OP" ] || OP="(no sim?)"
MCC=${OP:0:3}; MNC=${OP:3}
echo "[sim] numeric=$OP mcc=$MCC mnc=$MNC"

if [ "$MODE" = "--show" ]; then
  for p in persist.mtk.volte.enable persist.mtk_dynamic_ims_switch \
           persist.radio.imsallowmtsms ro.mtk_volte_support ro.mtk_ims_support; do
    printf '%-32s = %s\n' "$p" "$(getprop $p)"
  done
  echo "volte_vt_enabled  = $(S 'settings get global volte_vt_enabled' | tr -d '\r')"
  S "content query --uri content://telephony/carriers --projection _id,name,apn,type,mcc,mnc" \
    | grep "mcc=$MCC" | grep ",ims," || echo "(no IMS APN for this operator)"
  exit 0
fi

# --- 1. settings + props --------------------------------------------------
REVERT=0
[ "$MODE" = "--revert" ] && REVERT=1
VAL=1
[ $REVERT -eq 1 ] && VAL=0

echo "[1/4] settings global volte_vt_enabled=$VAL"
S "settings put global volte_vt_enabled $VAL"
for p in persist.mtk.volte.enable persist.mtk_dynamic_ims_switch persist.radio.imsallowmtsms; do
  before=$(getprop "$p")
  [ "$before" = "$VAL" ] && { echo "      $p already $VAL"; continue; }
  echo "      $p: ${before:-<empty>} -> $VAL"
  S "setprop $p $VAL"
done

# --- 2. IMS APN -----------------------------------------------------------
# The stock full-set has no type=ims row for Chinese operators, so the IMS PDN
# is never created and SIP REGISTER never happens.  This is the actual root
# cause of "mobile data works but calls/SMS do not".
HAS_IMS=$(S "content query --uri content://telephony/carriers --projection _id,name,apn,type,mcc,mnc" \
           | grep "mcc=$MCC" | grep "mnc=$MNC" | grep ",ims," || true)
if [ $REVERT -eq 1 ]; then
  if [ -n "$HAS_IMS" ]; then
    ROW_ID=$(echo "$HAS_IMS" | sed -n 's/.*_id=\([0-9]*\).*/\1/p' | head -1)
    echo "[2/4] deleting IMS APN row _id=$ROW_ID"
    S "content delete --uri content://telephony/carriers --where \"_id=$ROW_ID\""
  else
    echo "[2/4] no IMS APN row, nothing to revert"
  fi
elif [ -n "$HAS_IMS" ]; then
  echo "[2/4] IMS APN already present, skipping insert"
else
  echo "[2/4] inserting IMS APN (apn=ims type=ims mcc=$MCC mnc=$MNC)"
  S "content insert --uri content://telephony/carriers \
     --bind name:s:ims --bind apn:s:ims --bind type:s:ims \
     --bind mcc:s:$MCC --bind mnc:s:$MNC \
     --bind numeric:s:$MCC$MNC --bind user:s: --bind password:s: \
     --bind auth:i:0 --bind protocol:s:IPv4v6 --bind roaming_protocol:s:IPv4v6 \
     --bind carrier_enabled:b:1 --bind mmsproxy:s: --bind mmsport:s: \
     --bind mmsc:s: --bind mvno_type:s:none --bind mvno_match_data:s: \
     --bind bearer:i:-1 --bind profile_id:i:-1 --bind edit_status:i:1"
fi

# --- 3. reboot ------------------------------------------------------------
if [ "${NO_REBOOT:-0}" = "1" ]; then
  echo "[3/4] NO_REBOOT=1, skipping"
else
  echo "[3/4] rebooting (persist.* props and the APN db only take effect cleanly after boot)"
  $ADB reboot
fi

# --- 4. verify ------------------------------------------------------------
cat <<'EOS'
[4/4] after boot, verify with:
  adb shell su -c "getprop | grep -E 'mtk.volte|dynamic_ims|imsallowmtsms'"
  adb shell su -c "ip -6 addr show dev ccmni4"      # global IPv6, not just fe80::
  adb shell su -c "dumpsys telephony.registry" | grep -i "Capabilities: IMS"
  # 状态栏应出现 4G+HD；然后真的拨一次号码才算闭环
EOS
