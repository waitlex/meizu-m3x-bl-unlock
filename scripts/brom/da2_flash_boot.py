#!/usr/bin/env python3
# v79: flash the Magisk-patched boot image through the already-proven
# BROM -> kamakiri2 -> DA2 path.  Gates: host files must match known md5s,
# the live boot partition must byte-match the stock backup before writing,
# and the 16MiB readback must match the patched image.  No seccfg logic here.
import os
import sys
import hashlib
import traceback
from struct import pack, unpack

sys.path.insert(0, '/tmp')
import brom_base as v
import brom_kamakiri2 as w
import da2_sej_tools as u

LOG = v.log
hx = v.hx
hxi = v.hxi

STOCK = '/tmp/backup/boot.img'
PATCHED = '/tmp/magisk/magisk_patched.img'
STOCK_MD5 = '35b69eacec793901a625d84747951f93'
PATCHED_MD5 = '08f05c4657be72ab3effa0e8a108b700'
LIVE_COPY = '/tmp/backup/boot_live_before_flash.img'
BOOT_LEN = 0x1000000
BOOT_ADDR = 0x30C20000

rd32 = u.rd32
wr32 = u.wr32
find_part = u.find_part
stash_state = u.stash_state


def read_flash(mtk, addr, length, tag):
    data = mtk.daloader.readflash(addr=addr, length=length, filename="",
                                  parttype="user", display=False)
    data = bytes(data) if data is not None else b""
    LOG('V79_READ', tag, hxi(addr), 'len=%d' % len(data),
        'md5=%s' % hashlib.md5(data).hexdigest() if data else 'md5=-')
    return data


def file_ok(path, md5_want, size_want, tag):
    try:
        data = open(path, 'rb').read()
    except Exception as e:
        LOG('V79_FILE_EXC', tag, path, type(e).__name__, str(e)[:80])
        return None
    md5 = hashlib.md5(data).hexdigest()
    ok = (len(data) == size_want and md5 == md5_want)
    LOG('V79_FILE', tag, path, 'len=%d' % len(data), 'md5=%s' % md5, 'ok=%s' % ok)
    return data if ok else None


def v79_da_flow(mtk, da, label):
    log = LOG
    log('V79_DA_FLOW_START', label)
    mtk.daloader.patch = True
    try:
        res = mtk.daloader.upload_da(preloader=None)
    except Exception as e:
        log('V79_DA_UPLOAD_EXC', type(e).__name__, str(e)[:100])
        res = False
    log('V79_DA_UPLOAD', res)
    if not res:
        log('V79_DA_UPLOAD_FAILED')
        os._exit(4)
    try:
        mtk.daloader.writestate()
        log('V79_STATE_WRITTEN')
    except Exception as e:
        log('V79_STATE_WRITE_EXC', type(e).__name__, str(e)[:80])

    stock = file_ok(STOCK, STOCK_MD5, BOOT_LEN, 'stock')
    patched = file_ok(PATCHED, PATCHED_MD5, BOOT_LEN, 'patched')
    if stock is None or patched is None:
        log('V79_ABORT_FILE_GATE')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    if patched[:8] != b'ANDROID!' or patched[8:12] != stock[8:12]:
        log('V79_ABORT_HEADER', 'patched_kernel=%s' % hx(patched[8:12]),
            'stock_kernel=%s' % hx(stock[8:12]))
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    ks, rs = unpack('<I', stock[8:12])[0], unpack('<I', stock[16:20])[0]
    kp, rp = unpack('<I', patched[8:12])[0], unpack('<I', patched[16:20])[0]
    log('V79_PATCH_SHAPE', 'kernel %d->%d' % (ks, kp), 'ramdisk %d->%d' % (rs, rp))
    if kp != ks or rp <= rs:
        log('V79_ABORT_SHAPE')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    part = find_part(mtk, 'boot')
    if part is None:
        log('V79_NO_BOOT_PART')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    pagesize = mtk.config.pagesize or 512
    addr = part.sector * pagesize
    log('V79_BOOT_PART', 'sector=%d' % part.sector, 'sectors=%d' % part.sectors,
        'addr=%s' % hxi(addr))
    if part.sectors * pagesize < BOOT_LEN:
        log('V79_ABORT_PART_LAYOUT', 'addr=%s' % hxi(addr),
            'sectors=%d' % part.sectors)
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    live = read_flash(mtk, addr, BOOT_LEN, 'liveboot')
    if len(live) != BOOT_LEN:
        log('V79_LIVE_SHORT', len(live))
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    live_md5 = hashlib.md5(live).hexdigest()
    try:
        open(LIVE_COPY, 'wb').write(live)
        log('V79_LIVE_SAVED', LIVE_COPY)
    except Exception as e:
        log('V79_LIVE_SAVE_EXC', type(e).__name__, str(e)[:60])
    if live_md5 == PATCHED_MD5:
        log('V79_ALREADY_PATCHED')
        try:
            mtk.daloader.shutdown(bootmode=0)
            log('V79_RESET_SENT')
        except Exception as e:
            log('V79_RESET_FAIL', type(e).__name__, str(e)[:80])
        log('AGENT_V79_DONE', 'reason=already-patched')
        os._exit(0)
    log('V79_LIVE_MD5', live_md5, 'expect_stock=%s' % STOCK_MD5,
        'match=%s' % (live_md5 == STOCK_MD5))
    if live_md5 != STOCK_MD5:
        log('V79_ABORT_UNKNOWN_BOOT')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    try:
        ok = mtk.daloader.writeflash(addr=addr, length=len(patched), filename="",
                                     parttype="user", wdata=patched, display=True)
    except Exception as e:
        log('V79_WRITE_EXC', type(e).__name__, str(e)[:120])
        ok = False
    log('V79_WRITE_FLASH', hxi(addr), 'len=%d' % len(patched), 'ret=%s' % ok)
    if ok is not True:
        log('V79_WRITE_FAILED')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    rb = read_flash(mtk, addr, BOOT_LEN, 'readback')
    rb_md5 = hashlib.md5(rb).hexdigest() if len(rb) == BOOT_LEN else '-'
    rb_ok = rb_md5 == PATCHED_MD5
    log('V79_VERIFY', 'readback_md5=%s' % rb_md5, 'expect=%s' % PATCHED_MD5,
        'match=%s' % rb_ok)
    if not rb_ok:
        log('V79_VERIFY_FAILED_KEEP_ALIVE')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    try:
        mtk.daloader.shutdown(bootmode=0)
        log('V79_RESET_SENT')
    except Exception as e:
        log('V79_RESET_FAIL', type(e).__name__, str(e)[:80])
    log('AGENT_V79_DONE', 'reason=flashed')
    os._exit(0)


def main():
    v.start_stall_watchdog(600)
    LOG('AGENT_V79_START', 'uid=%d' % os.getuid())
    try:
        patcher = open(w.PATCHER_BIN, 'rb').read()
    except Exception as e:
        LOG('V79_BIN_EXC', type(e).__name__, str(e)[:80])
        os._exit(9)
    LOG('V79_BIN', 'patcher=%d' % len(patcher))
    stash_state()

    sess = w.connect('v79-a', 20.0)
    if sess is None:
        LOG('V79_NO_SESSION')
        LOG('AGENT_V79_DONE', 'reason=no-session')
        os._exit(9)
    LOG('V79_SESSION', 'el=%dms' % w.el())
    if not getattr(sess['mtk'].config, 'is_brom', False):
        LOG('V79_NOT_BROM_ABORT', 'is_brom=%s' % getattr(sess['mtk'].config, 'is_brom', None))
        LOG('AGENT_V79_DONE', 'reason=not-brom')
        os._exit(9)

    healthy = w.wait_k2(sess, 60.0, 'k2a')
    sess['healthy'] = healthy
    view = 0
    if healthy:
        view = w.view_check(sess['k2'])
        if view is None:
            view = 0

    v.da_flow = v79_da_flow
    LOG('V79_HOOK', 'da_flow=%s' % getattr(v.da_flow, '__name__', None))

    for var1 in (0xA, 0x9, 0xB):
        LOG('V79_ROUND', hxi(var1), 'healthy=%s' % healthy, 'el=%dms' % w.el())
        if w.patcher_shot(sess, patcher, var1, view) == 'da':
            os._exit(0)
        LOG('V79_ROUND_DEAD', hxi(var1))
        break

    LOG('AGENT_V79_DONE', 'reason=no-ack', 'el=%dms' % w.el())
    os._exit(9)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        LOG('V79_FATAL', tb.replace('\n', ' | ')[:400])
        try:
            open('/tmp/da2_flash_boot_fatal.txt', 'w').write(tb)
        except Exception:
            pass
        os._exit(9)
