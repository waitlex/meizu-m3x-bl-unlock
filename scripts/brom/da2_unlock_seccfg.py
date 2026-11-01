#!/usr/bin/env python3
# v78: fixes v77's crash (dump_and_scan signature) so the ROM-info hunt
# actually runs.  LK's seccfg transform (0x2ba34) reads the ROM info struct at
# a fixed RAM pointer, and if [g]==0x014D4D4D it loads g+0xC..+0x2B into
# SW_OTP before driver A.  mtkclient reads the same 32 bytes from the
# preloader image header at +0xC.  This GPT has no preloader partition, but
# the eMMC boot1 area is a strong candidate for the preloader, so boot1/boot2
# are dumped read-only first and scanned for the MMM\x01 header.  Candidate
# keys are replayed through driver A (LK-exact) + SW_OTP; only an exact
# reproduction of the stored seccfg hash may write seccfg.
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

SEC_BEFORE = '/tmp/backup/seccfg_before.bin'
SEC_NEW = '/tmp/backup/seccfg_new.bin'
SEC_AFTER = '/tmp/backup/seccfg_after.bin'
GPT_BACKUP = '/tmp/backup/gpt.bin'
STATE_FILE = '/tmp/backup/.state'
HITS_FILE = '/tmp/backup/rominfo_hits.txt'
KEYS_FILE = '/tmp/backup/rominfo_keys.txt'

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
R_SECINIT = SEJ_BASE + 0xBC

CFG1 = [0x9ED40400, 0x00E884A1, 0xE3F083BD, 0x2F4E6D8A]
RND = [0x2D44BB70, 0xA744D227, 0xD0A9864B, 0x83FFC244,
       0x7EC8266B, 0x43E80FB2, 0x01A6348A, 0x2067F9A0,
       0x54536405, 0xD546A6B1, 0x1CC3EC3A, 0xDE377A83]
SW_ASCII_KEY = b"25A1763A21BC854CD569DC23B4782B63"

MAGIC5 = b'\x4d\x4d\x4d\x01\x30'
MAGIC4 = b'\x4d\x4d\x4d\x01'

SCAN_SMALL = ['sec1', 'para', 'rstinfo', 'custom', 'persist', 'devinfo', 'frp', 'private',
              'nvcfg', 'expdb', 'flashinfo']
SMALL_CAP = 0x400000

rd32 = u.rd32
wr32 = u.wr32
find_part = u.find_part
poll_rdy = u.poll_rdy
stash_state = u.stash_state


class RegError(u.RegError):
    pass


def soft_rd32(xft, addr, tag, dflt=None):
    try:
        return rd32(xft, addr, tag)
    except Exception as e:
        LOG('V78_RD_EXC', tag, hxi(addr), type(e).__name__, str(e)[:60])
        return dflt


def read_flash(mtk, addr, length, tag, parttype="user"):
    data = mtk.daloader.readflash(addr=addr, length=length, filename="",
                                  parttype=parttype, display=False)
    data = bytes(data) if data is not None else b""
    LOG('V78_READ', tag, hxi(addr), 'len=%d' % len(data), 'head=%s' % hx(data[:0x20]))
    return data


def write_flash(mtk, addr, data):
    ok = mtk.daloader.writeflash(addr=addr, length=len(data), filename="",
                                 parttype="user", wdata=data, display=True)
    LOG('V78_WRITE_FLASH', hxi(addr), 'len=%d' % len(data), 'ret=%s' % ok)
    return ok is True


def set_otp(xft, key32, tag):
    key32 = bytes(key32)[:32].ljust(32, b'\x00')
    for i in range(8):
        wr32(xft, R_OTP0 + 4 * i, int.from_bytes(key32[4 * i:4 * i + 4], 'little'),
             tag + '/otp%d' % i)


def sej_blocks(xft, data, tag, single16=False):
    out = bytearray()
    step = 16
    for off in range(0, len(data), step):
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
        if single16:
            break
    wr32(xft, R_ACON2, 2, tag + '/done')
    for i in range(8):
        wr32(xft, R_AKEY0 + 4 * i, 0, tag + '/clr_key')
    return bytes(out[:len(data)])


def sej_v3(xft, data, encrypt, aconk_final, secinit, tag):
    """LK driver A (0x2fcec..0x2feb4 + 0x2ff5c), CLR precedes ACFG."""
    wr32(xft, CLK_REG, 0x20, tag + '/clk')
    for i in range(8):
        wr32(xft, R_AKEY0 + 4 * i, 0, tag + '/akey')
    wr32(xft, R_ACON, 2, tag + '/acon')
    wr32(xft, R_ACONK, 0x10, tag + '/aconk10')
    ak = soft_rd32(xft, R_ACONK, tag + '/aconk_rd', 0x10)
    wr32(xft, R_ACONK, (ak | 0x100) & 0xFFFFFFFF, tag + '/aconk100')
    wr32(xft, R_ACON2, 2, tag + '/clr')
    for i in range(4):
        wr32(xft, R_ACFG0 + 4 * i, CFG1[i], tag + '/pre_iv')
    if secinit is not None:
        wr32(xft, R_SECINIT, secinit, tag + '/secinit')
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
    out = sej_blocks(xft, data, tag)
    LOG('V78_SEJ_RUN', tag, 'r2k_polls=%s' % polls, 'in=%s' % data.hex(),
        'out=%s' % out.hex())
    return out


def sej_v4(xft, data, encrypt, tag):
    """mtkclient legacy V4 (no xor)."""
    wr32(xft, CLK_REG, 0x20, tag + '/clk')
    for i in range(8):
        wr32(xft, R_AKEY0 + 4 * i, 0, tag + '/akey')
    wr32(xft, R_ACON, 2, tag + '/acon')
    wr32(xft, R_ACONK, 0x10, tag + '/aconk10')
    ak = soft_rd32(xft, R_ACONK, tag + '/aconk_rd', 0x10)
    wr32(xft, R_ACONK, (ak | 0x100) & 0xFFFFFFFF, tag + '/aconk100')
    wr32(xft, R_ACON2, 2, tag + '/clr')
    for i in range(4):
        wr32(xft, R_ACFG0 + 4 * i, CFG1[i], tag + '/iv')
    si = soft_rd32(xft, R_SECINIT, tag + '/secinit_rd', 0)
    wr32(xft, R_SECINIT, (si | 2) & 0xFFFFFFFF, tag + '/secinit_set')
    ac2 = soft_rd32(xft, R_ACON2, tag + '/ac2_rd', 0)
    wr32(xft, R_ACON2, (ac2 | 0x40000000) & 0xFFFFFFFF, tag + '/ac2_b30')
    polls = -1
    st = 0
    for n in range(1, 101):
        st = rd32(xft, R_ACON2, tag + '/init_poll')
        if st > 0x80000000:
            polls = n
            break
    wr32(xft, R_SECINIT, si & 0xFFFFFFFE, tag + '/secinit_clr')
    wr32(xft, R_ACONK, 0x10, tag + '/aconk2')
    wr32(xft, R_ACON, 3 if encrypt else 2, tag + '/acon2')
    out = sej_blocks(xft, data, tag)
    LOG('V78_V4_RUN', tag, 'init_polls=%d' % polls, 'ac2_last=%s' % hxi(st),
        'in=%s' % data.hex(), 'out=%s' % out.hex())
    return out


def sej_v2(xft, data, encrypt, tag):
    """mtkclient V2: xor first 16B with CFG1 around the legacy transform."""
    buf = bytearray(data)
    if encrypt:
        for i in range(4):
            buf[4 * i:4 * i + 4] = (int.from_bytes(buf[4 * i:4 * i + 4], 'little') ^
                                    CFG1[i]).to_bytes(4, 'little')
    out = bytearray(sej_v4(xft, bytes(buf), encrypt, tag))
    if not encrypt:
        for i in range(4):
            out[4 * i:4 * i + 4] = (int.from_bytes(out[4 * i:4 * i + 4], 'little') ^
                                    CFG1[i]).to_bytes(4, 'little')
    LOG('V78_V2_RUN', tag, 'in=%s' % data.hex(), 'out=%s' % bytes(out).hex())
    return bytes(out)


def sej_drvb(xft, data, key32, iv16, encrypt, tag):
    """LK driver B (0x3010c/0x30320), CBC over whole input."""
    wr32(xft, CLK_REG, 0x20, tag + '/clk')
    for i in range(8):
        wr32(xft, R_AKEY0 + 4 * i, 0, tag + '/akey0')
    acon = soft_rd32(xft, R_ACON, tag + '/acon_rd', 2)
    acon = (acon & ~0x30) | 0x20
    wr32(xft, R_ACON, acon, tag + '/acon256')
    acnk = soft_rd32(xft, R_ACONK, tag + '/aconk_rd', 0)
    wr32(xft, R_ACONK, acnk & ~0x10, tag + '/aconk_nobk')
    wr32(xft, R_ACON2, 2, tag + '/clr')
    for i in range(8):
        wr32(xft, R_AKEY0 + 4 * i, int.from_bytes(key32[4 * i:4 * i + 4], 'little'),
             tag + '/key')
    for i in range(4):
        wr32(xft, R_ACFG0 + 4 * i, int.from_bytes(iv16[4 * i:4 * i + 4], 'little'),
             tag + '/iv')
    ac2 = soft_rd32(xft, R_ACON2, tag + '/ac2_rd', 0)
    wr32(xft, R_ACON2, ac2 | 2, tag + '/ac2_or2')
    acon = soft_rd32(xft, R_ACON, tag + '/acon_rd2', 2)
    wr32(xft, R_ACON, (acon | 1) if encrypt else (acon & 0xFFFFFFFE), tag + '/acon_enc')
    out = sej_blocks(xft, data, tag)
    LOG('V78_DRVB_RUN', tag, 'acon=%s' % hxi(acon), 'in=%s' % data.hex(),
        'out=%s' % out.hex())
    return out


def with_otp(key32, core, label):
    def run(xft, data, encrypt):
        set_otp(xft, key32, label)
        try:
            return core(xft, data, encrypt, label)
        finally:
            set_otp(xft, bytes(32), label + '/clr')
    return run


def lk3_core(xft, data, encrypt, tag):
    return sej_v3(xft, data, encrypt, 3, None, tag)


def v4_core(xft, data, encrypt, tag):
    return sej_v4(xft, data, encrypt, tag)


def v2_core(xft, data, encrypt, tag):
    return sej_v2(xft, data, encrypt, tag)


def scan_blob(blob):
    hits = []
    for pat, name in ((MAGIC5, 'mm30'), (MAGIC4, 'mm')):
        idx = 0
        while True:
            i = blob.find(pat, idx)
            if i < 0:
                break
            idx = i + 1
            hits.append((name, i, blob[i + 0xc:i + 0x2c], blob[i:i + 0x30]))
    return hits


def dump_boot(mtk):
    parts = []
    for pname in ('boot1', 'boot2'):
        size = 0x400000
        try:
            emmc = mtk.daloader.daconfig.storage.emmc
            sz = getattr(emmc, 'boot1_size', 0)
            if sz:
                size = min(size, sz)
        except Exception:
            pass
        try:
            data = read_flash(mtk, 0, size, pname, parttype=pname)
        except Exception as e:
            LOG('V78_BOOT_EXC', pname, type(e).__name__, str(e)[:100])
            continue
        if not data:
            LOG('V78_BOOT_EMPTY', pname)
            continue
        path = '/tmp/backup/%s.bin' % pname
        try:
            open(path, 'wb').write(data)
            LOG('V78_DUMP_SAVED', path, 'len=%d' % len(data),
                'md5=%s' % hashlib.md5(data).hexdigest(),
                'nonzero=%d' % (len(data) - data.count(0)))
        except Exception as e:
            LOG('V78_DUMP_SAVE_EXC', pname, type(e).__name__, str(e)[:60])
        parts.append((pname, data))
    return parts


def dump_small(mtk, names):
    parts = []
    for name in names:
        part = find_part(mtk, name)
        if part is None:
            LOG('V78_SMALL_MISSING', name)
            continue
        pagesize = mtk.config.pagesize or 512
        addr = part.sector * pagesize
        size = min(part.sectors * pagesize, SMALL_CAP)
        try:
            data = read_flash(mtk, addr, size, name)
        except Exception as e:
            LOG('V78_SMALL_EXC', name, type(e).__name__, str(e)[:80])
            continue
        if not data:
            continue
        path = '/tmp/backup/%s.bin' % name
        try:
            open(path, 'wb').write(data)
            LOG('V78_DUMP_SAVED', path, 'len=%d' % len(data),
                'md5=%s' % hashlib.md5(data).hexdigest())
        except Exception as e:
            LOG('V78_DUMP_SAVE_EXC', name, type(e).__name__, str(e)[:60])
        parts.append((name, data))
    return parts


def collect_keys(parts):
    keys = []
    lines = []
    for name, blob in parts:
        hits = scan_blob(blob)
        for hi, (kind, off, key, ctx) in enumerate(hits[:8]):
            line = '%-10s %-4s off=0x%08x key=%s ctx=%s' % (
                name, kind, off, key.hex(), ctx.hex())
            lines.append(line)
            LOG('V78_ROMINFO_HIT', line[:300])
            if len(key) == 32 and key != bytes(32):
                keys.append(('%s%dA' % (name, hi), key))
            if off + 0x2b9 <= len(blob):
                raw = blob[off + 0x2a9:off + 0x2b9]
                if raw != bytes(16):
                    inc = bytes((b + 1) & 0xFF for b in raw)
                    keys.append(('%s%dB' % (name, hi), raw + inc))
                    lines.append('%-10s %-4s off=0x%08x keyB=%s' % (
                        name, kind, off, (raw + inc).hex()))
    dedup = []
    seen = set()
    for label, k in keys:
        if k not in seen:
            seen.add(k)
            dedup.append((label, k))
    try:
        open(HITS_FILE, 'w').write('\n'.join(lines) + '\n')
        open(KEYS_FILE, 'w').write('\n'.join('%s %s' % (l, k.hex()) for l, k in dedup) + '\n')
        LOG('V78_HITS_SAVED', HITS_FILE, 'lines=%d' % len(lines), 'keys=%d' % len(dedup))
    except Exception as e:
        LOG('V78_HITS_SAVE_EXC', type(e).__name__, str(e)[:80])
    return dedup


def find_winner(xft, sha, stored, keys):
    a5 = bytes([0xA5]) * 32
    otp_cands = [('zero', bytes(32)), ('a5pat', a5), ('ascii', SW_ASCII_KEY)]
    for i, (label, k) in enumerate(keys[:10]):
        otp_cands.append((label, k))
        otp_cands.append((label + '_inc', k[:16] + bytes((b + 1) & 0xFF for b in k[:16])))

    base = None
    for name, key in otp_cands:
        fn = with_otp(key, lk3_core, 'otp%s' % name)
        try:
            enc = fn(xft, sha, True)
            if base is None:
                base = enc
            LOG('V78_OTP_TEST', name, 'key=%s' % key.hex(), 'effect=%s' % (enc != base),
                'out=%s' % enc.hex())
            if enc == stored:
                LOG('V78_MATCH', 'lk3+otp:' + name, 'dir=enc')
                return 'lk3+otp:' + name, fn
            dec = fn(xft, stored, False)
            if dec == sha:
                LOG('V78_MATCH_DEC_ONLY', 'lk3+otp:' + name)
        except Exception as e:
            LOG('V78_OTP_EXC', name, type(e).__name__, str(e)[:80])

    for core_name, core in (('v4', v4_core), ('v2', v2_core)):
        cands = [('zero', bytes(32))]
        for label, k in keys[:3]:
            cands.append((label, k))
        for name, key in cands:
            fn = with_otp(key, core, '%s%s' % (core_name, name))
            try:
                enc = fn(xft, sha, True)
                LOG('V78_%s_TEST' % core_name.upper(), name, 'out=%s' % enc.hex())
                if enc == stored:
                    LOG('V78_MATCH', '%s+otp:%s' % (core_name, name), 'dir=enc')
                    return '%s+otp:%s' % (core_name, name), fn
                dec = fn(xft, stored, False)
                if dec == sha:
                    LOG('V78_MATCH_DEC_ONLY', '%s+otp:%s' % (core_name, name))
            except Exception as e:
                LOG('V78_%s_EXC' % core_name.upper(), name, type(e).__name__, str(e)[:80])

    ivs = [('zero', bytes(16)),
           ('cfg1', b''.join(w_.to_bytes(4, 'little') for w_ in CFG1[:4]))]
    drv_keys = [('zero', bytes(32)), ('ascii', SW_ASCII_KEY)]
    for label, k in keys[:6]:
        drv_keys.append((label, k))
    for kname, key in drv_keys:
        for iname, iv in ivs:
            for enc_dir in (False, True):
                tag = 'drvb:%s:%s:%s' % (kname, iname, 'enc' if enc_dir else 'dec')
                try:
                    src = sha if enc_dir else stored
                    ref = stored if enc_dir else sha
                    out = sej_drvb(xft, src, key, iv, enc_dir, tag)
                    if out == ref:
                        LOG('V78_DRVB_MATCH', tag, 'out=%s' % out.hex())
                        fn = (lambda kk, ii: (lambda xft, data, encrypt:
                                              sej_drvb(xft, data, kk, ii, encrypt, 'drvb-final')))(key, iv)
                        return tag, fn
                except Exception as e:
                    LOG('V78_DRVB_EXC', tag, type(e).__name__, str(e)[:80])
    return None, None


def v78_da_flow(mtk, da, label):
    log = LOG
    log('V78_DA_FLOW_START', label)
    mtk.daloader.patch = True
    try:
        res = mtk.daloader.upload_da(preloader=None)
    except Exception as e:
        log('V78_DA_UPLOAD_EXC', type(e).__name__, str(e)[:100])
        res = False
    log('V78_DA_UPLOAD', res)
    if not res:
        log('V78_DA_UPLOAD_FAILED')
        os._exit(4)
    try:
        mtk.daloader.writestate()
        log('V78_STATE_WRITTEN', STATE_FILE)
    except Exception as e:
        log('V78_STATE_WRITE_EXC', type(e).__name__, str(e)[:80])

    gpt_ok = os.path.exists(GPT_BACKUP) and os.path.getsize(GPT_BACKUP) > 0
    sec_ok = os.path.exists(SEC_BEFORE) and os.path.getsize(SEC_BEFORE) > 0
    log('V78_GATE', 'gpt=%s' % gpt_ok, 'seccfg_before=%s' % sec_ok)
    if not (gpt_ok and sec_ok):
        log('V78_ABORT_BACKUP_GATE')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    part = find_part(mtk, 'seccfg')
    if part is None:
        log('V78_NO_SECCFG_PART')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    pagesize = mtk.config.pagesize or 512
    addr = part.sector * pagesize
    live = read_flash(mtk, addr, 0x200, 'live')
    if len(live) < 0x40:
        log('V78_LIVE_SHORT', len(live))
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    hdr, stored = bytes(live[:0x1c]), bytes(live[0x1c:0x3c])
    sha = hashlib.sha256(hdr).digest()
    magic, ver, size, lock_state, dm_state, sboot, endflag = unpack('<IIIIIII', hdr)
    log('V78_HDR', 'lock=%d' % lock_state, 'dm=%d' % dm_state, 'ver=%d' % ver,
        'size=%s' % hxi(size))
    log('V78_HASH', 'sha=%s' % sha.hex(), 'stored=%s' % stored.hex())
    if magic != 0x4D4D4D4D or endflag != 0x45454545:
        log('V78_BAD_SECCFG_HEADER')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    if lock_state == 3:
        log('V78_ALREADY_UNLOCKED')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    xft = mtk.daloader.xft
    if xft is None or not hasattr(xft, 'custom_readregister'):
        log('V78_NO_XFT')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    parts = []
    try:
        parts = dump_boot(mtk)
    except Exception as e:
        log('V78_BOOT_SCAN_EXC', type(e).__name__, str(e)[:100])
    keys = collect_keys(parts)
    if not keys:
        try:
            small = dump_small(mtk, SCAN_SMALL)
            keys = collect_keys(small)
        except Exception as e:
            log('V78_SMALL_SCAN_EXC', type(e).__name__, str(e)[:100])
    log('V78_KEYS', 'count=%d' % len(keys),
        'list=%s' % ','.join('%s:%s' % (l, k.hex()[:16]) for l, k in keys[:6]))

    winner, fn = find_winner(xft, sha, stored, keys)
    if winner is None:
        log('V78_PROBE_NOMATCH_ABORT', 'no seccfg write performed')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    log('V78_WINNER', winner)

    hdr_new = pack('<IIIIIII', 0x4D4D4D4D, ver, 0x3c, 3, dm_state, sboot, 0x45454545)
    new_sha = hashlib.sha256(hdr_new).digest()
    try:
        new_hash = fn(xft, new_sha, True)
        back = fn(xft, new_hash, False)
    except Exception as e:
        log('V78_SELFCHECK_EXC', type(e).__name__, str(e)[:120])
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    ok_self = back == new_sha
    log('V78_SELFCHECK', 'new_sha=%s' % new_sha.hex(),
        'new_hash=%s' % new_hash.hex(), 'roundtrip=%s' % ok_self)
    if not ok_self:
        log('V78_SELFCHECK_FAIL_ABORT')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    data_new = hdr_new + new_hash
    data_new += b'\x00' * (0x200 - len(data_new))
    try:
        open(SEC_NEW, 'wb').write(data_new)
    except Exception:
        pass
    log('V78_NEW', 'hdr=%s' % hdr_new.hex(), 'hash=%s' % new_hash.hex())
    if not write_flash(mtk, addr, data_new):
        log('V78_WRITE_FAILED')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    log('V78_SECCFG_WRITE_OK')

    rb = read_flash(mtk, addr, 0x200, 'readback')
    rb_ok = rb[:0x3c] == data_new[:0x3c]
    log('V78_SECCFG_VERIFY', 'match=%s' % rb_ok, 'hdr=%s' % hx(rb[:0x1c]),
        'hash=%s' % hx(rb[0x1c:0x3c]))
    try:
        rt = fn(xft, bytes(rb[0x1c:0x3c]), False)
        rt_ok = rt == hashlib.sha256(bytes(rb[:0x1c])).digest()
        log('V78_SECCFG_VERIFY_CRYPT', 'ok=%s' % rt_ok)
    except Exception as e:
        rt_ok = False
        log('V78_VERIFY_CRYPT_EXC', type(e).__name__, str(e)[:100])
    if not (rb_ok and rt_ok):
        log('V78_VERIFY_FAILED_KEEP_ALIVE')
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)

    try:
        da.da_read('seccfg', 'user', SEC_AFTER)
        log('V78_SECCFG_AFTER_OK', os.path.getsize(SEC_AFTER))
    except Exception as e:
        log('V78_SECCFG_AFTER_FAIL', type(e).__name__, str(e)[:80])

    try:
        mtk.daloader.shutdown(bootmode=0)
        log('V78_RESET_SENT')
    except Exception as e:
        log('V78_RESET_FAIL', type(e).__name__, str(e)[:80])
    log('AGENT_V78_DONE', 'reason=unlocked', 'variant=%s' % winner)
    os._exit(0)


def main():
    v.start_stall_watchdog(600)
    LOG('AGENT_V78_START', 'uid=%d' % os.getuid())
    try:
        patcher = open(w.PATCHER_BIN, 'rb').read()
    except Exception as e:
        LOG('V78_BIN_EXC', type(e).__name__, str(e)[:80])
        os._exit(9)
    LOG('V78_BIN', 'patcher=%d' % len(patcher))
    stash_state()

    sess = w.connect('v78-a', 20.0)
    if sess is None:
        LOG('V78_NO_SESSION')
        LOG('AGENT_V78_DONE', 'reason=no-session')
        os._exit(9)
    LOG('V78_SESSION', 'el=%dms' % w.el())
    if not getattr(sess['mtk'].config, 'is_brom', False):
        LOG('V78_NOT_BROM_ABORT', 'is_brom=%s' % getattr(sess['mtk'].config, 'is_brom', None))
        LOG('AGENT_V78_DONE', 'reason=not-brom')
        os._exit(9)

    healthy = w.wait_k2(sess, 60.0, 'k2a')
    sess['healthy'] = healthy
    view = 0
    if healthy:
        view = w.view_check(sess['k2'])
        if view is None:
            view = 0

    v.da_flow = v78_da_flow
    LOG('V78_HOOK', 'da_flow=%s' % getattr(v.da_flow, '__name__', None))

    for var1 in (0xA, 0x9, 0xB):
        LOG('V78_ROUND', hxi(var1), 'healthy=%s' % healthy, 'el=%dms' % w.el())
        if w.patcher_shot(sess, patcher, var1, view) == 'da':
            os._exit(0)
        LOG('V78_ROUND_DEAD', hxi(var1))
        break

    LOG('AGENT_V78_DONE', 'reason=no-ack', 'el=%dms' % w.el())
    os._exit(9)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        LOG('V78_FATAL', tb.replace('\n', ' | ')[:400])
        try:
            open('/tmp/da2_unlock_seccfg_fatal.txt', 'w').write(tb)
        except Exception:
            pass
        os._exit(9)
