#!/usr/bin/env bash
# Build the host-side environment used by scripts/brom/*.py on macOS.
# Everything lives under /tmp because root cannot read ~/Documents (macOS TCC).
set -euo pipefail

MTK_DIR=/tmp/mtknew
VENV=/tmp/rootvenv
LIBUSB_DIR=/tmp/libusb
PYPI=https://pypi.tuna.tsinghua.edu.cn/simple
LIBUSB_VER=1.0.27

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 1; }; }
need curl
need python3
need make
need cc

mkdir -p /tmp/backup

# --- 1. mtkclient snapshot -------------------------------------------------
# github.com is DNS-poisoned on this host; codeload works, gitee is a fallback.
if [ ! -d "$MTK_DIR" ]; then
  echo "[1/4] fetching mtkclient"
  if curl -fL --max-time 300 -o /tmp/mtk.tar.gz \
       https://codeload.github.com/bkerler/mtkclient/tar.gz/refs/heads/main; then
    tar -xzf /tmp/mtk.tar.gz -C /tmp
    mv /tmp/mtkclient-main "$MTK_DIR"
  else
    echo "      codeload failed, falling back to gitee mirror"
    git clone --depth 1 https://gitee.com/wandering_snow/mtkclient.git "$MTK_DIR"
  fi
else
  echo "[1/4] $MTK_DIR already present"
fi

for p in mtkclient/payloads/generic_patcher_payload.bin \
         mtkclient/payloads/mt6757_payload.bin; do
  [ -f "$MTK_DIR/$p" ] || { echo "missing payload: $MTK_DIR/$p"; exit 1; }
done

# --- 2. venv --------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  echo "[2/4] creating venv"
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install -U pip setuptools wheel -i "$PYPI"
  "$VENV/bin/pip" install pyusb pycryptodomex pyserial capstone -i "$PYPI"
else
  echo "[2/4] $VENV already present"
fi

# --- 3. libusb ------------------------------------------------------------
# pyusb cannot find libusb by default here; either homebrew's or a source build.
if [ -e "$LIBUSB_DIR/libusb-1.0.dylib" ]; then
  echo "[3/4] using $LIBUSB_DIR"
elif ls /opt/homebrew/lib/libusb-1.0.dylib /usr/local/lib/libusb-1.0.dylib >/dev/null 2>&1; then
  echo "[3/4] using homebrew libusb"
else
  echo "[3/4] building libusb $LIBUSB_VER from source"
  curl -fL -o /tmp/libusb.tar.gz \
    "https://sourceforge.net/projects/libusb/files/libusb-1.0/libusb-$LIBUSB_VER/libusb-$LIBUSB_VER.tar.gz/download"
  tar -xzf /tmp/libusb.tar.gz -C /tmp
  (cd "/tmp/libusb-$LIBUSB_VER" && ./configure --prefix="$LIBUSB_DIR" && make -j4 && make install)
fi

# --- 4. self check --------------------------------------------------------
LIBPATH=$(dirname "$(ls /opt/homebrew/lib/libusb-1.0.dylib /usr/local/lib/libusb-1.0.dylib \
        "$LIBUSB_DIR"/libusb-1.0.dylib 2>/dev/null | head -1)")
echo "[4/4] libusb at $LIBPATH"
DYLD_FALLBACK_LIBRARY_PATH="$LIBPATH" "$VENV/bin/python" - <<'PY'
import usb.backend.libusb1 as b
print("libusb backend:", b.get_backend())
import usb.core
print("devices visible:", len(list(usb.core.find(find_all=True))))
PY

cat <<EOF

Done. Run the brom agents like this (all of them import each other from /tmp):

  cp scripts/brom/*.py /tmp/
  sudo DYLD_FALLBACK_LIBRARY_PATH=$LIBPATH $VENV/bin/python /tmp/da2_unlock_seccfg.py

Only BROM (0e8d:0003) needs root; DA2 (0e8d:2001) works as a normal user:

  DYLD_FALLBACK_LIBRARY_PATH=$LIBPATH $VENV/bin/python /tmp/da2_attach.py
EOF
