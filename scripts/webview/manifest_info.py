"""Read versionName / versionCode / package from an APK's binary AndroidManifest."""
import struct, sys, zipfile
from axml_patch import parse_pool

START_EL = 0x0102
TYPE_STRING, TYPE_INT_DEC = 0x03, 0x10


def main(path):
    b = zipfile.ZipFile(path).read('AndroidManifest.xml')
    pool = parse_pool(b, 8)
    S = pool['strings']
    p = 8 + struct.unpack('<I', b[12:16])[0]
    while p + 8 <= len(b):
        typ, _, size = struct.unpack('<HHI', b[p:p + 8])
        if size == 0:
            break
        if typ == START_EL:
            name = S[struct.unpack('<I', b[p + 20:p + 24])[0]]
            if name != 'manifest':
                p += size
                continue
            acnt = struct.unpack('<H', b[p + 28:p + 30])[0]
            out = {}
            for i in range(acnt):
                q = p + 36 + 20 * i
                an, raw, meta, data = struct.unpack('<IIII', b[q + 4:q + 20])
                key = S[an]
                dt = (meta >> 24) & 0xFF
                if raw != 0xFFFFFFFF:
                    out[key] = S[raw]
                elif dt == TYPE_STRING:
                    out[key] = S[data]
                elif dt == TYPE_INT_DEC:
                    out[key] = str(data if data < 0x80000000 else data - 0x100000000)
                else:
                    out[key] = 'type=%d val=%d' % (dt, data)
            print('PKG', out.get('package'))
            print('VERSIONNAME', out.get('versionName'))
            print('VERSIONCODE', out.get('versionCode'))
            return
        p += size
    print('MANIFEST_NOT_FOUND')


if __name__ == '__main__':
    main(sys.argv[1])
