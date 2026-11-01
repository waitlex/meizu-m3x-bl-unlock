#!/usr/bin/env python3
"""Read/write one eMMC partition through an already-running DA2 (0e8d:2001).

The DA2 stage is only reachable after a BROM run left it alive: run
`da2_unlock_seccfg.py`, let it log `DA_KEPT_ALIVE_FOR_ATTACH` (first pass, no
backups yet) or `shutdown` itself, then attach here.  Nothing in this file
re-implements the exploit: it reuses the two primitives that were proven on
this device -- `da2_attach.init_da()` for the DA2 handshake, and
`da2_sej_tools.find_part()` + `mtk.daloader.readflash/writeflash` for raw
access by GPT sector.

  usage: da2_partition.py read  boot   /tmp/backup/boot.img
  usage: da2_partition.py write boot   /tmp/magisk/magisk_patched.img --yes

`write` refuses to run unless a non-empty backup of the same partition already
exists in /tmp/backup, so an interrupted session cannot leave the device with
no way back.  This tool was used for reads on the M3X; the write path is the
same primitive v79 used, but rolling back boot/seccfg with it has not itself
been executed end-to-end on this device -- treat it as untested and keep the
on-disk backups.
"""
import os
import sys
import logging
import hashlib

sys.path.insert(0, '/tmp')
sys.path.insert(0, '/tmp/mtknew')

import brom_base as v
import da2_sej_tools as u
import da2_attach as d

LOG = v.log
hxi = v.hxi

BACKUP_DIR = '/tmp/backup'


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        os._exit(2)
    op, part, path = sys.argv[1], sys.argv[2], sys.argv[3]
    assume_yes = '--yes' in sys.argv[4:]

    if op not in ('read', 'write'):
        LOG('PART_BAD_OP', op)
        os._exit(2)
    if op == 'write' and not os.path.exists(path):
        LOG('PART_NO_INPUT', path)
        os._exit(2)

    candidates = [os.path.join(BACKUP_DIR, n) for n in
                  (part + '.bin', part + '.img', part + '_before.bin',
                   part + '_before.img')]
    backup = next((p for p in candidates
                   if os.path.exists(p) and os.path.getsize(p) > 0), None)
    if op == 'write':
        if backup is None:
            LOG('PART_ABORT_NO_BACKUP', ','.join(candidates))
            os._exit(3)
        if not assume_yes:
            LOG('PART_ABORT_NEED_YES', 'pass --yes to write %s (backup=%s)'
                % (part, backup))
            os._exit(3)
        LOG('PART_BACKUP_GATE', backup, os.path.getsize(backup))

    config = d.build_config()
    mtk = d.attach(config)
    if mtk is None:
        LOG('PART_DONE', 'reason=no-port')
        os._exit(2)
    da = d.init_da(mtk)
    if da is None:
        LOG('PART_DONE', 'reason=no-da')
        os._exit(4)
    mtk.daloader.patch = True

    entry = u.find_part(mtk, part)
    if entry is None:
        LOG('PART_NOT_FOUND', part)
        LOG('DA_KEPT_ALIVE_FOR_ATTACH')
        os._exit(5)
    pagesize = mtk.config.pagesize or 512
    addr = entry.sector * pagesize
    size = entry.sectors * pagesize
    LOG('PART_FOUND', part, 'sector=%d' % entry.sector, 'addr=%s' % hxi(addr),
        'size=%d' % size)

    if op == 'read':
        data = u.read_flash(mtk, addr, size, 'read:' + part)
        if len(data) != size:
            LOG('PART_READ_SHORT', len(data), 'want=%d' % size)
            os._exit(6)
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        LOG('PART_READ_OK', path, len(data), hashlib.md5(data).hexdigest())
    else:
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) > size:
            LOG('PART_ABORT_TOO_BIG', len(data), 'part=%d' % size)
            os._exit(3)
        ok = mtk.daloader.writeflash(addr=addr, length=len(data), filename="",
                                     parttype="user", wdata=data, display=True)
        LOG('PART_WRITE', hxi(addr), len(data), 'ret=%s' % ok)
        if ok is not True:
            LOG('PART_WRITE_FAILED')
            LOG('DA_KEPT_ALIVE_FOR_ATTACH')
            os._exit(6)
        rb = u.read_flash(mtk, addr, len(data), 'readback:' + part)
        match = hashlib.md5(rb).hexdigest() == hashlib.md5(data).hexdigest()
        LOG('PART_VERIFY', 'match=%s' % match)
        if not match:
            LOG('DA_KEPT_ALIVE_FOR_ATTACH')
            os._exit(6)

    try:
        mtk.daloader.shutdown(bootmode=0)
        LOG('PART_RESET_SENT')
    except Exception as e:
        LOG('PART_RESET_FAIL', type(e).__name__, str(e)[:80])
    LOG('PART_DONE', 'op=%s part=%s' % (op, part))
    os._exit(0)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        u.LOG if False else None
        LOG('PART_FATAL', type(e).__name__, str(e)[:200])
        try:
            import traceback
            open('/tmp/da2_partition_fatal.txt', 'w').write(traceback.format_exc())
        except Exception:
            pass
        os._exit(9)
