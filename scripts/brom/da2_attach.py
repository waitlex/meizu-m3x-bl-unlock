#!/usr/bin/env python3
# Attach to the DA2 stage2 that v69 left running (0e8d:2001) after the host-side
# SIGSEGV inside DAXFlash.reinit()'s "reconnect with higher speed".
# K2/ACK/handshake/DA1/EMI/DA2 upload already succeeded in that session, so this
# script only re-opens the USB endpoint (reconnect disabled), reinitializes DA2
# state, backs up partitions, then gates seccfg unlock on backup completeness.
import os
import sys
import time
import logging
import traceback

sys.path.insert(0, '/tmp')
sys.path.insert(0, '/tmp/mtknew')

import brom_base as v
from mtkclient.config.mtk_config import MtkConfig
from mtkclient.Library.mtk_class import Mtk
from mtkclient.Library.DA.mtk_da_handler import DaHandler

LOG = v.log
hx = v.hx
hxi = v.hxi


def fatal(tag, exc=None):
    tb = traceback.format_exc() if exc is not None else ''
    LOG(tag, tb.replace('\n', ' | ')[:400])
    try:
        open('/tmp/da2_fatal.txt', 'w').write(tb)
    except Exception:
        pass


def build_config():
    config = MtkConfig(loglevel=logging.INFO, gui=None, guiprogress=None)
    args = v.Args(ptype='kamakiri2', preloader=None, directory='/tmp/backup',
                  loglevel=logging.INFO, debugmode=False, noreconnect=True)
    v.ArgHandler(args, config)
    config.reconnect = False
    config.init_hwcode(0x551)
    config.is_brom = False
    config.target_config = {'sbc': True, 'sla': True, 'daa': True, 'epp': False,
                            'cert': False, 'memread': True, 'memwrite': True,
                            'cmdC8': True}
    return config


def attach(config):
    mtk = Mtk(config=config, loglevel=logging.INFO)
    if not mtk.port.wait_for_device(max_attempts=6, interval=0.5, wait=True):
        LOG('DA2_NO_PORT')
        return None
    LOG('DA2_PORT_OK', 'pid=%s' % hxi(getattr(mtk.port.cdc, 'pid', None)),
        'iface=%s' % getattr(mtk.port.cdc, 'interface', None))
    return mtk


def init_da(mtk):
    try:
        mtk.daloader.daconfig.setup()
    except Exception as e:
        LOG('DA2_SETUP_EXC', type(e).__name__, str(e)[:80])
    mtk.daloader.set_da()
    da = mtk.daloader.da
    if da is None:
        LOG('DA2_SET_DA_FAILED', repr(mtk.daloader.flashmode))
        return None
    LOG('DA2_SET_DA', type(da).__name__)
    try:
        da.reinit(display=True)
    except Exception as e:
        fatal('DA2_REINIT_EXC', e)
        return None
    LOG('DA2_REINIT_OK',
        'emmc=%s' % getattr(getattr(da, 'emmc', None), 'id', None),
        'cid=%s' % hx(getattr(getattr(da, 'emmc', None), 'cid', None)),
        'user=%s' % hxi(getattr(getattr(da, 'emmc', None), 'user_size', None)),
        'dram=%s' % hxi(getattr(getattr(da, 'dram', None), 'size', None)))
    return da


def main():
    probe_only = '--probe' in sys.argv
    LOG('DA2_ATTACH_START', 'uid=%d' % os.getuid(), 'probe_only=%s' % probe_only)
    config = build_config()
    mtk = attach(config)
    if mtk is None:
        LOG('DA2_DONE', 'reason=no-port')
        os._exit(2)
    da = init_da(mtk)
    if da is None:
        LOG('DA2_DONE', 'reason=no-da')
        os._exit(4)
    mtk.daloader.patch = True
    if probe_only:
        LOG('DA2_DONE', 'reason=probe-ok')
        os._exit(0)

    dh = DaHandler(mtk, logging.INFO)
    os.makedirs('/tmp/backup', exist_ok=True)
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
            dh.da_read(name, 'user', path)
            size = os.path.getsize(path) if os.path.exists(path) else 'missing'
            LOG('DA2_BACKUP_OK', name, size)
        except Exception as e:
            LOG('DA2_BACKUP_FAIL', name, type(e).__name__, str(e)[:80])

    gptp = '/tmp/backup/gpt.bin'
    secp = '/tmp/backup/seccfg_before.bin'
    backup_ok = (os.path.exists(gptp) and os.path.getsize(gptp) > 0
                 and os.path.exists(secp) and os.path.getsize(secp) > 0)
    LOG('DA2_BACKUP_GATE', 'gpt=%s seccfg=%s ok=%s' % (
        os.path.getsize(gptp) if os.path.exists(gptp) else 'missing',
        os.path.getsize(secp) if os.path.exists(secp) else 'missing', backup_ok))
    if not backup_ok:
        LOG('DA2_UNLOCK_SKIPPED_BACKUP_MISSING')
    else:
        try:
            with open(secp, 'rb') as f:
                head = f.read(16)
            LOG('DA2_SECCFG_HEAD', hx(head))
        except Exception:
            pass
        try:
            res = mtk.daloader.seccfg('unlock')
            LOG('DA2_SECCFG_RESULT', repr(res))
        except Exception as e:
            fatal('DA2_SECCFG_EXC', e)
        try:
            dh.da_read('seccfg', 'user', '/tmp/backup/seccfg_after.bin')
            LOG('DA2_SECCFG_AFTER_OK',
                os.path.getsize('/tmp/backup/seccfg_after.bin'))
            with open('/tmp/backup/seccfg_after.bin', 'rb') as f:
                LOG('DA2_SECCFG_AFTER_HEAD', hx(f.read(16)))
        except Exception as e:
            LOG('DA2_SECCFG_AFTER_FAIL', type(e).__name__, str(e)[:80])

    try:
        mtk.daloader.shutdown(bootmode=0)
        LOG('DA2_RESET_SENT')
    except Exception as e:
        LOG('DA2_RESET_FAIL', type(e).__name__, str(e)[:80])
    LOG('DA2_DONE', 'finished')
    os._exit(0)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fatal('DA2_FATAL', e)
        os._exit(9)
