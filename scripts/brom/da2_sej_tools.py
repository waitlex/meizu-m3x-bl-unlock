#!/usr/bin/env python3
# v74: stop guessing SEJ variants. LK's own seccfg verify code was reversed
# from /tmp/backup/lk.bin:
#   0x2baf8  sec_cfg_get_status: sha256(seccfg, 0x1c) -> sej_encrypt -> memcmp(seccfg+0x1c, 0x20)
#   0x2fcec  sej init: 0x10001084=0x20, AKEY=0, ACON=2, ACONK=0x110, ACON2=2,
#            ACFG=CFG1, 3x R2K rounds (RND pattern, poll ACON2 bit15), ACON2=2,
#            ACFG=CFG1, ACON=3/2, ACONK=0
#   0x2febc  run: ASRC=block, ACON2=1, poll bit15, read AOUT
# So gate: sej_encrypt(sha256(live_hdr)) must equal the hash already on the
# device. Only after that match do we write a new seccfg.
import os
import sys
import hashlib
import traceback
from struct import pack, unpack

sys.path.insert(0, '/tmp')
import brom_base as v
import brom_kamakiri2 as w

LOG = v.log
hx = v.hx
hxi = v.hxi

SEC_BEFORE = '/tmp/backup/seccfg_before.bin'
SEC_NEW = '/tmp/backup/seccfg_new.bin'
SEC_AFTER = '/tmp/backup/seccfg_after.bin'
GPT_BACKUP = '/tmp/backup/gpt.bin'
STATE_FILE = '/tmp/backup/.state'

SEJ_BASE = 0x1000A000
CLK_REG = 0x10001084
R_ACON = SEJ_BASE + 0x04
R_ACON2 = SEJ_BASE + 0x08
R_ACONK = SEJ_BASE + 0x0C
R_ASRC0 = SEJ_BASE + 0x10
R_AKEY0 = SEJ_BASE + 0x20
R_ACFG0 = SEJ_BASE + 0x40
R_AOUT0 = SEJ_BASE + 0x50
R_OTP0 = SEJ_BASE + 0x60
R_SECINIT_NEW = SEJ_BASE + 0xBC

CFG1 = [0x9ED40400, 0x00E884A1, 0xE3F083BD, 0x2F4E6D8A]
RND = [0x2D44BB70, 0xA744D227, 0xD0A9864B, 0x83FFC244,
       0x7EC8266B, 0x43E80FB2, 0x01A6348A, 0x2067F9A0,
       0x54536405, 0xD546A6B1, 0x1CC3EC3A, 0xDE377A83]
VARIANTS = {
    # name: (clock write, final ACONK, write SECINIT0_new=1)
    'lk': (True, 0x0, False),
    'lk_ac110': (True, 0x110, False),
    'lk_noclock': (False, 0x0, False),
    'mtk': (False, 0x110, True),
}


class RegError(Exception):
    pass


def rd32(xft, addr, tag=''):
    val = xft.custom_readregister(addr, 1)
    if not isinstance(val, int):
        raise RegError('rd32 %s %s -> %r' % (hxi(addr), tag, val))
    return val & 0xFFFFFFFF


def wr32(xft, addr, val, tag=''):
    if not xft.custom_writeregister(addr, int(val) & 0xFFFFFFFF):
        raise RegError('wr32 %s <= %s %s' % (hxi(addr), hxi(val), tag))


def stash_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        os.replace(STATE_FILE, STATE_FILE + '.prev')
        LOG('V74_STATE_STASHED', STATE_FILE)
    except Exception:
        try:
            os.remove(STATE_FILE)
            LOG('V74_STATE_REMOVED', STATE_FILE)
        except Exception:
            pass


def find_part(mtk, name):
    try:
        table = mtk.daloader.get_partition_data(parttype="user")
    except Exception as e:
        LOG('V74_PART_TABLE_EXC', type(e).__name__, str(e)[:80])
        return None
    for e in table:
        if getattr(e, 'name', '').lower() == name.lower():
            return e
    return None


def read_flash(mtk, addr, length, tag):
    data = mtk.daloader.readflash(addr=addr, length=length, filename="",
                                  parttype="user", display=False)
    data = bytes(data) if data is not None else b""
    LOG('V74_READ', tag, hxi(addr), 'len=%d' % len(data), 'head=%s' % hx(data[:0x40]))
    return data


def write_flash(mtk, addr, data):
    ok = mtk.daloader.writeflash(addr=addr, length=len(data), filename="",
                                 parttype="user", wdata=data, display=True)
    LOG('V74_WRITE_FLASH', hxi(addr), 'len=%d' % len(data), 'ret=%s' % ok)
    return ok is True


def poll_rdy(xft, tag, limit=100):
    st = 0
    for n in range(1, limit + 1):
        st = rd32(xft, R_ACON2, tag)
        if st & 0x8000:
            return n, st
    return -1, st


def sej_lk(xft, data, encrypt, variant):
    clock, aconk_final, secinit = VARIANTS[variant]
    tag = '%s/%s' % (variant, 'enc' if encrypt else 'dec')
    if clock:
        wr32(xft, CLK_REG, 0x20, tag + '/clk')
        LOG('V74_CLK', tag, 'reg=%s' % hxi(rd32(xft, CLK_REG, tag + '/clk')))
    for i in range(8):
        wr32(xft, R_AKEY0 + 4 * i, 0, tag + '/akey')
    wr32(xft, R_ACON, 2, tag + '/acon')
    wr32(xft, R_ACONK, 0x110, tag + '/aconk')
    wr32(xft, R_ACON2, 2, tag + '/clr')
    if secinit:
        wr32(xft, R_SECINIT_NEW, 1, tag + '/secinit')
    # LK writes ACFG0=0x400 before the 3 R2K rounds, then CFG1 after them.
    pre_cfg = [0x400, CFG1[1], CFG1[2], CFG1[3]]
    for i in range(4):
        wr32(xft, R_ACFG0 + 4 * i, pre_cfg[i], tag + '/pre_iv')
    polls = []
    for r in range(3):
        for i in range(4):
            wr32(xft, R_ASRC0 + 4 * i, RND[r * 4 + i], tag + '/rnd')
        wr32(xft, R_ACON2, 1, tag + '/start')
        n, st = poll_rdy(xft, tag + '/r2k')
        polls.append(n)
        if n < 0:
            raise RegError('r2k_rdy_timeout round=%d ac=%s' % (r, hxi(st)))
    wr32(xft, R_ACON2, 2, tag + '/clr2')
    for i in range(4):
        wr32(xft, R_ACFG0 + 4 * i, CFG1[i], tag + '/iv2')
    wr32(xft, R_ACON, 3 if encrypt else 2, tag + '/acon2')
    wr32(xft, R_ACONK, aconk_final, tag + '/aconk2')
    out = bytearray()
    for off in range(0, len(data), 16):
        blk = data[off:off + 16].ljust(16, b'\x00')
        for i in range(4):
            wr32(xft, R_ASRC0 + 4 * i, int.from_bytes(blk[i * 4:i * 4 + 4], 'little'),
                 tag + '/blk')
        wr32(xft, R_ACON2, 1, tag + '/aes')
        n, st = poll_rdy(xft, tag + '/aes')
        if n < 0:
            raise RegError('aes_rdy_timeout off=%d ac=%s' % (off, hxi(st)))
        for i in range(4):
            out += rd32(xft, R_AOUT0 + 4 * i, tag + '/aout').to_bytes(4, 'little')
    wr32(xft, R_ACON2, 2, tag + '/done')
    for i in range(8):
        wr32(xft, R_AKEY0 + 4 * i, 0, tag + '/clr_key')
        wr32(xft, R_OTP0 + 4 * i, 0, tag + '/clr_otp')
    LOG('V74_SEJ_RUN', tag, 'in=%s' % data.hex(), 'r2k_polls=%s' % polls,
        'out=%s' % bytes(out).hex())
    return bytes(out[:len(data)])


def find_winner(xft, sha, stored):
    """The device already holds a valid seccfg, so the exact LK procedure must
    reproduce its hash. Try the LK path first, then two near variants."""
    results = []
    for variant in ('lk', 'lk_ac110', 'lk_noclock', 'mtk'):
        try:
            enc = sej_lk(xft, sha, True, variant)
            if enc == stored:
                LOG('V74_MATCH', variant, 'dir=enc')
                return variant, 'enc', results
            LOG('V74_NOMATCH', variant, 'dir=enc', 'got=%s' % enc.hex())
        except Exception as e:
            LOG('V74_VARIANT_EXC', variant, 'enc', type(e).__name__, str(e)[:100])
        try:
            dec = sej_lk(xft, stored, False, variant)
            if dec == sha:
                LOG('V74_MATCH_DEC_ONLY', variant, 'not a write gate')
            else:
                LOG('V74_NOMATCH', variant, 'dir=dec', 'got=%s' % dec.hex())
        except Exception as e:
            LOG('V74_VARIANT_EXC', variant, 'dec', type(e).__name__, str(e)[:100])
    return None, None, results


def v74_da_flow(mtk, da, label):
    log = LOG
    log('V74_DA_FLOW_START', label)
    mtk.daloader.patch = True
    try:
        res = mtk.daloader.upload_da(preloader=None)
    except Exception as e:
        log('V74_DA_UPLOAD_EXC', type(e).__name__, str(e)[:100])
        res = False
    log('V74_DA_UPLOAD', res)
    if not res:
        log('V74_DA_UPLOAD_FAILED')
        os._exit(4)
    try:
        mtk.daloader.writestate()
        log('V74_STATE_WRITTEN', STATE_FILE)
    except Exception as e:
        log('V74_STATE_WRITE_EXC', type(e).__name__, str(e)[:80])

    gpt_ok = os.path.exists(GPT_BACKUP) and os.path.getsize(GPT_BACKUP) > 0
    sec_ok = os.path.exists(SEC_BEFORE) and os.path.getsize(SEC_BEFORE) > 0
    log('V74_GATE', 'gpt=%s' % gpt_ok, 'seccfg_before=%s' % sec_ok)
    if not (gpt_ok and sec_ok):
        log('V74_ABORT_BACKUP_GATE')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    part = find_part(mtk, 'seccfg')
    if part is None:
        log('V74_NO_SECCFG_PART')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    pagesize = mtk.config.pagesize or 512
    addr = part.sector * pagesize
    log('V74_PART', 'sector=%d' % part.sector, 'sectors=%d' % part.sectors,
        'pagesize=%d' % pagesize, 'addr=%s' % hxi(addr))

    live = read_flash(mtk, addr, 0x200, 'live')
    if len(live) < 0x40:
        log('V74_LIVE_SHORT', len(live))
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    hdr, stored = bytes(live[:0x1c]), bytes(live[0x1c:0x3c])
    sha = hashlib.sha256(hdr).digest()
    magic, ver, size, lock_state, dm_state, sboot, endflag = unpack('<IIIIIII', hdr)
    log('V74_HDR', 'magic=%s' % hxi(magic), 'ver=%d' % ver, 'size=%s' % hxi(size),
        'lock=%d' % lock_state, 'dm=%d' % dm_state, 'sboot=%d' % sboot,
        'end=%s' % hxi(endflag))
    log('V74_HASH', 'sha=%s' % sha.hex(), 'stored=%s' % stored.hex())
    if magic != 0x4D4D4D4D or endflag != 0x45454545:
        log('V74_BAD_SECCFG_HEADER')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    if lock_state == 3:
        log('V74_ALREADY_UNLOCKED')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    try:
        before = open(SEC_BEFORE, 'rb').read(0x3c)
        log('V74_BEFORE_MATCH', 'backup_vs_live=%s' % (before == hdr + stored))
    except Exception as e:
        log('V74_BEFORE_READ_EXC', type(e).__name__, str(e)[:80])

    xft = mtk.daloader.xft
    if xft is None or not hasattr(xft, 'custom_readregister'):
        log('V74_NO_XFT')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    # ASRC0 is a HACC source register, not reliably readable; use ACONK for a
    # write/readback probe. It is diagnostic only -- the oracle below is the gate.
    try:
        base_acon = rd32(xft, R_ACON, 'probe')
        base_acon2 = rd32(xft, R_ACON2, 'probe')
        base_aconk = rd32(xft, R_ACONK, 'probe')
        log('V74_SEJ_BASE', 'acon=%s' % hxi(base_acon), 'acon2=%s' % hxi(base_acon2),
            'aconk=%s' % hxi(base_aconk))
        wr32(xft, R_ACONK, 0x110, 'probe')
        rb = rd32(xft, R_ACONK, 'probe')
        wr32(xft, R_ACONK, 0, 'probe')
        log('V74_ACONK_RW', 'readback=%s' % hxi(rb), 'stick=%s' % (rb == 0x110))
    except Exception as e:
        log('V74_SEJ_PROBE_EXC', type(e).__name__, str(e)[:120])

    variant, direction, _ = find_winner(xft, sha, stored)
    if variant is None:
        log('V74_PROBE_NOMATCH_ABORT', 'no seccfg write performed')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    log('V74_WINNER', variant, 'dir=%s' % direction)

    hdr_new = pack('<IIIIIII', 0x4D4D4D4D, ver, 0x3c, 3, dm_state, sboot, 0x45454545)
    new_sha = hashlib.sha256(hdr_new).digest()
    try:
        new_hash = sej_lk(xft, new_sha, True, variant)
        back = sej_lk(xft, new_hash, False, variant)
    except Exception as e:
        log('V74_SELFCHECK_EXC', type(e).__name__, str(e)[:120])
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    ok_self = back == new_sha
    log('V74_SELFCHECK', 'new_sha=%s' % new_sha.hex(),
        'new_hash=%s' % new_hash.hex(), 'roundtrip=%s' % ok_self)
    if not ok_self:
        log('V74_SELFCHECK_FAIL_ABORT')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    data_new = hdr_new + new_hash
    data_new += b'\x00' * (0x200 - len(data_new))
    log('V74_NEW', 'hdr=%s' % hdr_new.hex(), 'hash=%s' % new_hash.hex())
    try:
        open(SEC_NEW, 'wb').write(data_new)
        log('V74_NEW_SAVED', SEC_NEW, len(data_new))
    except Exception as e:
        log('V74_NEW_SAVE_EXC', type(e).__name__, str(e)[:80])

    if not write_flash(mtk, addr, data_new):
        log('V74_WRITE_FAILED')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    log('V74_SECCFG_WRITE_OK')

    rb = read_flash(mtk, addr, 0x200, 'readback')
    rb_ok = rb[:0x3c] == data_new[:0x3c]
    log('V74_SECCFG_VERIFY', 'match=%s' % rb_ok, 'hdr=%s' % hx(rb[:0x1c]),
        'hash=%s' % hx(rb[0x1c:0x3c]))
    try:
        rt = sej_lk(xft, bytes(rb[0x1c:0x3c]), False, variant)
        rt_ok = rt == hashlib.sha256(bytes(rb[:0x1c])).digest()
        log('V74_SECCFG_VERIFY_CRYPT', 'ok=%s' % rt_ok)
    except Exception as e:
        rt_ok = False
        log('V74_VERIFY_CRYPT_EXC', type(e).__name__, str(e)[:100])
    if not (rb_ok and rt_ok):
        log('V74_VERIFY_FAILED_KEEP_ALIVE')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    try:
        da.da_read('seccfg', 'user', SEC_AFTER)
        log('V74_SECCFG_AFTER_OK', os.path.getsize(SEC_AFTER))
    except Exception as e:
        log('V74_SECCFG_AFTER_FAIL', type(e).__name__, str(e)[:80])

    try:
        mtk.daloader.shutdown(bootmode=0)
        log('V74_RESET_SENT')
    except Exception as e:
        log('V74_RESET_FAIL', type(e).__name__, str(e)[:80])
    log('AGENT_V74_DONE', 'reason=unlocked', 'variant=%s' % variant)
    os._exit(0)


def main():
    v.start_stall_watchdog(600)
    LOG('AGENT_V74_START', 'uid=%d' % os.getuid())
    try:
        patcher = open(w.PATCHER_BIN, 'rb').read()
    except Exception as e:
        LOG('V74_BIN_EXC', type(e).__name__, str(e)[:80])
        os._exit(9)
    LOG('V74_BIN', 'patcher=%d' % len(patcher))
    stash_state()

    sess = w.connect('v74-a', 20.0)
    if sess is None:
        LOG('V74_NO_SESSION')
        LOG('AGENT_V74_DONE', 'reason=no-session')
        os._exit(9)
    LOG('V74_SESSION', 'el=%dms' % w.el())
    if not getattr(sess['mtk'].config, 'is_brom', False):
        LOG('V74_NOT_BROM_ABORT', 'is_brom=%s' % getattr(sess['mtk'].config, 'is_brom', None))
        LOG('AGENT_V74_DONE', 'reason=not-brom')
        os._exit(9)

    healthy = w.wait_k2(sess, 60.0, 'k2a')
    sess['healthy'] = healthy
    view = 0
    if healthy:
        view = w.view_check(sess['k2'])
        if view is None:
            view = 0

    v.da_flow = v74_da_flow
    LOG('V74_HOOK', 'da_flow=%s' % getattr(v.da_flow, '__name__', None))

    for var1 in (0xA, 0x9, 0xB):
        LOG('V74_ROUND', hxi(var1), 'healthy=%s' % healthy, 'el=%dms' % w.el())
        if w.patcher_shot(sess, patcher, var1, view) == 'da':
            os._exit(0)
        LOG('V74_ROUND_DEAD', hxi(var1))
        break

    LOG('AGENT_V74_DONE', 'reason=no-ack', 'el=%dms' % w.el())
    os._exit(9)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        LOG('V74_FATAL', tb.replace('\n', ' | ')[:400])
        try:
            open('/tmp/da2_sej_tools_fatal.txt', 'w').write(tb)
        except Exception:
            pass
        os._exit(9)
