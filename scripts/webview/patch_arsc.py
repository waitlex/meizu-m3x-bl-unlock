#!/usr/bin/env python3
"""原位改 resources.arsc 的 package 名（UTF-16LE, 256B 字段），不改变长度/偏移。"""
import struct, sys, shutil, zipfile, zlib

TARGET = "com.android.webview"

def find_pkg_chunk(arsc):
    typ, hsz, size = struct.unpack_from("<HHI", arsc, 0)
    assert typ == 0x0002, hex(typ)
    off = hsz
    res = []
    while off + 8 <= size:
        ctyp, chsz, csize = struct.unpack_from("<HHI", arsc, off)
        if csize < 8:
            break
        if ctyp == 0x0200:
            pid = struct.unpack_from("<I", arsc, off + 8)[0]
            raw = arsc[off + 12: off + 12 + 256]
            name = raw.decode("utf-16-le", "ignore").split("\x00")[0]
            res.append((off, chsz, pid, name))
        off += csize
    return res

def main(apk_in, apk_out):
    shutil.copyfile(apk_in, apk_out)
    zf = zipfile.ZipFile(apk_out)
    zi = zf.getinfo("resources.arsc")
    assert zi.compress_type == 0, "arsc not stored"
    zf.close()

    with open(apk_out, "r+b") as f:
        f.seek(zi.header_offset)
        lh = f.read(30)
        sig, ver, flags, comp, t, d, crc, cs, us, fnl, exl = struct.unpack("<IHHHHHIIIHH", lh)
        assert sig == 0x04034b50 and comp == 0
        data_off = zi.header_offset + 30 + fnl + exl
        f.seek(data_off)
        arsc = bytearray(f.read(zi.file_size))
        assert len(arsc) == zi.file_size

        pkgs = find_pkg_chunk(arsc)
        print("package chunks:", pkgs)
        assert len(pkgs) == 1, pkgs
        off, chsz, pid, old = pkgs[0]
        new_raw = TARGET.encode("utf-16-le") + b"\x00\x00"
        assert len(new_raw) <= 256
        arsc[off + 12: off + 12 + 256] = new_raw.ljust(256, b"\x00")
        new_crc = zlib.crc32(bytes(arsc)) & 0xFFFFFFFF
        f.seek(data_off)
        f.write(arsc)

        # 更新 central directory 中的 CRC
        f.seek(0)
        blob = bytearray(f.read())
        cd_off = None
        pos = blob.find(b"PK\x01\x02")
        while pos != -1:
            nlen, elen, clen = struct.unpack_from("<HHH", blob, pos + 28)
            name = blob[pos + 46: pos + 46 + nlen].decode("utf-8", "ignore")
            if name == "resources.arsc":
                cd_off = pos
                break
            pos = blob.find(b"PK\x01\x02", pos + 4)
        assert cd_off is not None
        struct.pack_into("<I", blob, cd_off + 16, new_crc)
        struct.pack_into("<I", blob, zi.header_offset + 14, new_crc)
        f.seek(0)
        f.write(blob)

    zf = zipfile.ZipFile(apk_out)
    b = zf.read("resources.arsc")
    zf.close()
    print("verify:", find_pkg_chunk(bytearray(b)))
    ok = any(n == TARGET for _, _, _, n in find_pkg_chunk(bytearray(b)))
    print("OK" if ok else "FAIL")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
