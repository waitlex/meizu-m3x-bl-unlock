#!/usr/bin/env python3
# v70: v69 proved the full chain works: K2 -> ACK -> handshake -> DA1 -> EMI
#  (DX18MB rec0) -> DRAM setup -> stage2 -> DA SLA disabled. The host then died
#  with SIGSEGV inside DAXFlash.reinit()'s "Reconnecting to stage2 with higher
#  speed" (port.close/device.reset + reopen in the same process). v70 disables
#  that reconnect (ArgHandler noreconnect=True in v50_open) and keeps the DA2
#  alive instead of resetting when the backup gate fails. Nothing else changed.
import os
import sys
import time
from struct import pack, unpack

sys.path.insert(0, '/tmp')
import brom_base as v

LOG = v.log
hx = v.hx
hxi = v.hxi
ACK = b'\xa1\xa2\xa3\xa4'

VID, PID = 0x0E8D, 0x0003
WDT = 0x10007000
ARM = WDT + 0x50
A00 = 0x100A00
SCR = 0x100D00
ANCHOR_ADDR = 0x100E00
ANCHOR_VAL = 0x3d367098
ANCHOR2_ADDR = 0x100E40
ANCHOR2_VAL = 0xd078e31a
PATCHER_BIN = '/tmp/mtknew/mtkclient/payloads/generic_patcher_payload.bin'
PRELOADER = '/tmp/mtknew/mtkclient/loader/Preloader/preloader_s21v57c2k_dg_m1t.bin'
T0 = time.time()


def el():
    return int((time.time() - T0) * 1000)


def u32(b):
    if b is not None and len(b) == 4:
        return unpack('<I', b)[0]
    return None


def read_m(k2, call, n, tag, mode=2):
    try:
        d = k2.access(call, n, None, True, mode)
    except Exception as e:
        LOG('V70_R_EXC', tag, hxi(call), 'm%d' % mode, type(e).__name__, str(e)[:60])
        return None
    return bytes(d) if d is not None else None


def write_m(k2, call, val, tag, check=True, mode=3):
    try:
        k2.access(call, 4, pack('<I', val & 0xFFFFFFFF), check, mode)
        return True
    except Exception as e:
        LOG('V70_W_EXC', tag, hxi(call), 'm%d' % mode, type(e).__name__, str(e)[:60])
        return False


def d4w(pre, addr, val, tag):
    try:
        ok = pre.write32(addr, [val & 0xFFFFFFFF])
    except Exception as e:
        LOG('V70_D4_EXC', tag, hxi(addr), type(e).__name__, str(e)[:60])
        return False
    LOG('V70_D4', tag, hxi(addr), hxi(val), 'ret=%s' % ok)
    return ok is True


def present():
    try:
        return v.usb.core.find(idVendor=VID, idProduct=PID) is not None
    except Exception:
        return False


def dev_addr():
    try:
        d = v.usb.core.find(idVendor=VID, idProduct=PID)
        if d is None:
            return None
        return (d.bus, d.address)
    except Exception:
        return None


def wait_reenum(tag='', timeout=30.0):
    start = dev_addr()
    gone = start is None
    t0 = time.time()
    while time.time() - t0 < timeout:
        cur = dev_addr()
        if cur is None:
            if not gone:
                LOG('V70_REENUM_GONE', tag, 'at_ms=%d' % el())
                gone = True
        else:
            if gone:
                LOG('V70_REENUM_BACK', tag, 'addr=%s' % repr(cur), 'ms=%d' % el())
                return 'back'
        time.sleep(0.15)
    LOG('V70_REENUM_TIMEOUT', tag, 'gone=%s' % gone)
    return 'gone' if gone else 'still'


def close_sess(sess):
    try:
        v.v50_close(sess)
    except Exception:
        pass


def connect(tag, wait_s=45.0):
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if present():
            s = v.v50_open(tag)
            if s is not None:
                return s
        time.sleep(1.0)
    return None


def wait_k2(sess, budget=60.0, tag='k2'):
    pre = sess['pre']
    base = sess['k2']
    combos = [(8, '0200'), (9, '0200'), (8, '02ff'), (9, '02ff')]
    t0 = time.time()
    i = 0
    while time.time() - t0 < budget:
        i += 1
        if time.time() - t0 < budget * 0.5:
            combo = combos[0]
        else:
            combo = combos[i % len(combos)]
        d4w(pre, ARM, 0x11223344, 'prime')
        kk = base
        if combo != (base.offset, base.trigger):
            try:
                kk = v.K2(sess['mtk'], sess['dev'], pre, sess['ptr_da'],
                          offset=combo[0], trigger=combo[1])
                kk.lc = base.lc
                kk.idx = base.idx
            except Exception as e:
                LOG('V70_K2_MK_EXC', type(e).__name__, str(e)[:50])
                kk = base
        w = write_m(kk, SCR, 0xDEADBEEF, 'kp-w', mode=3)
        got = u32(read_m(kk, SCR, 4, 'kp-r', mode=2))
        ok = w and got == 0xDEADBEEF
        LOG('V70_K2_TRY', tag, 'n=%d' % i, 'el=%dms' % el(),
            'off=%d' % combo[0], 'trig=%s' % combo[1], 'ok=%s' % ok,
            'r=%s' % hxi(got))
        if ok:
            base.offset = combo[0]
            base.trigger = combo[1]
            LOG('V70_K2_HEALTHY', tag, 'n=%d' % i, 'el=%dms' % el(),
                'off=%d' % combo[0], 'trig=%s' % combo[1])
            return True
        time.sleep(1.0)
    LOG('V70_K2_DEAD', tag, 'tries=%d' % i, 'el=%dms' % el())
    return False


def view_check(k2):
    d0 = u32(read_m(k2, ANCHOR_ADDR, 4, 'vc0', mode=2))
    view = None
    if d0 == ANCHOR_VAL:
        view = 0
    else:
        for delta in (0x40, -0x40, 0x80, -0x80, 0x4, -0x4):
            d = u32(read_m(k2, ANCHOR_ADDR + delta, 4, 'vcd', mode=2))
            if d == ANCHOR_VAL:
                view = delta
                break
    if view is None:
        LOG('V70_VIEW_UNKNOWN', 'a00=%s' % hxi(d0))
        return None
    d1 = u32(read_m(k2, ANCHOR2_ADDR + view, 4, 'vc1', mode=2))
    LOG('V70_VIEW_OK', 'view=%+d' % view, 'anchor2=%s' % hxi(d1),
        'anchor2_ok=%s' % (d1 == ANCHOR2_VAL))
    return view


def write_payload(k2, phys, data, tag, view=0):
    total = len(data)
    bad = 0
    t0 = time.time()
    for off in range(0, total, 0x100):
        if v.DEVICE_GONE:
            break
        chunk = data[off:off + 0x100]
        if len(chunk) % 4:
            chunk += b'\x00' * ((-len(chunk)) % 4)
        call = phys + off + view
        ok = False
        try:
            k2.access(call, len(chunk), chunk, True, 3)
            rb = k2.access(call, len(chunk), None, True, 2)
            ok = rb is not None and bytes(rb) == chunk
        except Exception as e:
            LOG('V70_PW_CHUNK_EXC', tag, hxi(call), type(e).__name__, str(e)[:50])
        if not ok:
            wbad = 0
            for i in range(0, len(chunk), 4):
                word = chunk[i:i + 4]
                wv = unpack('<I', word)[0]
                if not write_m(k2, call + i, wv, tag + '-w', mode=3):
                    wbad += 1
                    continue
                if read_m(k2, call + i, 4, tag + '-r', mode=2) != word:
                    wbad += 1
            LOG('V70_PW_WORDS', tag, hxi(call), 'bad=%d' % wbad)
            bad += wbad
        if off % 0x400 == 0:
            LOG('V70_PW_PROG', tag, '%d/%d' % (off + len(chunk), total),
                'bad=%d' % bad)
    LOG('V70_PW_DONE', tag, hxi(phys), 'bytes=%d' % total, 'bad=%d' % bad,
        'secs=%.1f' % (time.time() - t0))
    return bad == 0


def arm(pre, target):
    try:
        ok = pre.write32(ARM, [v.revdword(target)])
        LOG('V70_ARM', hxi(target), 'rev=%s' % hxi(v.revdword(target)),
            'ret=%s' % ok)
    except Exception as e:
        LOG('V70_ARM_EXC', type(e).__name__, str(e)[:60])
        return False
    for i in range(0xF):
        a = ARM - (0xF - i) * 4
        try:
            pre.read32(a, 0xF - i + 1)
        except Exception as e:
            LOG('V70_CHAIN_EXC', i, hxi(a), type(e).__name__, str(e)[:40])
    LOG('V70_CHAIN_DONE')
    return True


def trigger(var1=0xA, ms=1300):
    t0 = time.time()
    try:
        udev = v.usb.core.find(idVendor=VID, idProduct=PID)
        if udev is None:
            return 'nodev', 0
        try:
            udev._ctx.managed_claim_interface = lambda *a, **k: None
        except Exception:
            pass
        udev.ctrl_transfer(0xA1, 0, 0, var1, 0, timeout=ms)
        rc = 'ok'
    except v.usb.core.USBError as e:
        s = str(e).lower()
        rc = 'timeout' if ('timed out' in s or 'timeout' in s) else 'err'
        LOG('V70_TRIG_EXC', hxi(var1), type(e).__name__, str(e)[:80])
    except Exception as e:
        rc = 'err'
        LOG('V70_TRIG_FIND_EXC', hxi(var1), type(e).__name__, str(e)[:60])
    ms = int((time.time() - t0) * 1000)
    LOG('V70_TRIG', hxi(var1), rc, 'ms=%d' % ms)
    return rc, ms


def plant_payload(sess, payload, tag, view):
    if not sess.get('healthy'):
        LOG('V70_PLANT_SKIP', tag, 'k2-unhealthy')
        return False
    ok = write_payload(sess['k2'], A00, payload, tag, view)
    head = read_m(sess['k2'], A00 + view, 16, tag + '-head', mode=2)
    head_ok = head is not None and head == payload[:16]
    LOG('V70_PLANT', tag, 'ok=%s' % ok, 'head_ok=%s' % head_ok, hx(head))
    return head_ok


def upstream_trigger(sess, payload, var1, tag):
    mtk, pre = sess['mtk'], sess['pre']
    arm(pre, A00)
    v.flush_ep(mtk)
    v.RXLOG.clear()
    e0 = v.v50_e0(mtk, payload, tag + '-e0')
    LOG('V70_E0', tag, 'ok=%s' % e0)
    v.flush_ep(mtk)
    v.RXLOG.clear()
    trigger(var1)
    return e0


def emi_status(mtk):
    try:
        dc = mtk.daloader.daconfig
        dc.extract_emi(PRELOADER)
        n = len(dc.emi) if dc.emi else None
        LOG('V70_EMI', 'file=%s' % os.path.basename(PRELOADER),
            'ver=%s' % getattr(dc, 'emiver', None), 'len=%s' % n)
        return n is not None
    except Exception as e:
        LOG('V70_EMI_EXC', type(e).__name__, str(e)[:80])
        return False


def install_upload_patch(mtk):
    orig = mtk.daloader.upload_da

    def patched(preloader=None):
        LOG('V70_UPLOAD_DA', 'preloader=%s' % os.path.basename(PRELOADER))
        return orig(preloader=PRELOADER)

    mtk.daloader.upload_da = patched
    LOG('V70_UPLOAD_PATCHED')


def patcher_shot(sess, patcher, var1, view):
    mtk, da = sess['mtk'], sess['da']
    tag = 'patcher-%s' % hxi(var1)
    plant_payload(sess, patcher, tag, view)
    upstream_trigger(sess, patcher, var1, tag)
    got = v.collect(mtk, 6.0)
    LOG('V70_RX', hxi(var1), hx(got[:96]) if got else None)
    if ACK in (got or b'') or v.ack_seen():
        LOG('V70_ACK', hx(bytes(v.RXLOG[:64])))
        emi_status(mtk)
        install_upload_patch(mtk)
        v.try_handshake_and_da(mtk, da, 'v70')
        return 'da'
    LOG('V70_NO_ACK', hxi(var1), 'rxlog=%s' % hx(bytes(v.RXLOG[-48:])))
    return 'noack'


def main():
    v.start_stall_watchdog(300)
    LOG('AGENT_V70_START', 'uid=%d' % os.getuid())
    try:
        patcher = open(PATCHER_BIN, 'rb').read()
        preloader = open(PRELOADER, 'rb').read()
    except Exception as e:
        LOG('V70_BIN_EXC', type(e).__name__, str(e)[:80])
        os._exit(9)
    LOG('V70_BINS', 'patcher=%d' % len(patcher), hx(patcher[:16]),
        'preloader=%d' % len(preloader))

    sess = connect('v70-a', 20.0)
    if sess is None:
        LOG('V70_NO_SESSION')
        LOG('AGENT_V70_DONE', 'reason=no-session')
        os._exit(9)
    LOG('V70_SESSION_A', 'el=%dms' % el())
    LOG('V70_RECONNECT', 'reconnect=%s' % getattr(sess['mtk'].daloader.daconfig, 'reconnect', None))

    healthy = wait_k2(sess, 60.0, 'k2a')
    sess['healthy'] = healthy
    view = 0
    if healthy:
        view = view_check(sess['k2'])
        if view is None:
            view = 0

    for var1 in (0xA, 0x9, 0xB):
        LOG('V70_ROUND', hxi(var1), 'healthy=%s' % healthy, 'el=%dms' % el())
        if patcher_shot(sess, patcher, var1, view) == 'da':
            os._exit(0)
        LOG('V70_ROUND_DEAD', hxi(var1), 'no ACK, session spent')
        break

    LOG('AGENT_V70_DONE', 'reason=no-ack', 'el=%dms' % el())
    os._exit(9)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        LOG('V70_FATAL', tb.replace('\n', ' | ')[:400])
        try:
            open('/tmp/agent_fatal.txt', 'w').write(tb)
        except Exception:
            pass
        os._exit(9)
