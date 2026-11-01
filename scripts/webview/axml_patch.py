import struct, sys, zipfile

TYPE_XML = 0x0003
TYPE_STRING_POOL = 0x0001

def parse_pool(b, p):
    typ, hsz, size = struct.unpack('<HHI', b[p:p+8])
    assert typ == TYPE_STRING_POOL, hex(typ)
    cnt, stycnt, flags, sstart, stystart = struct.unpack('<IIIII', b[p+8:p+28])
    utf8 = bool(flags & 0x100)
    offs = list(struct.unpack('<%dI' % cnt, b[p+28:p+28+4*cnt]))
    base = p + sstart
    strs = []
    for o in offs:
        q = base + o
        if utf8:
            n = b[q]; q += 1
            if n & 0x80:
                n = ((n & 0x7f) << 8) | b[q]; q += 1
            n2 = b[q]; q += 1
            if n2 & 0x80:
                n2 = ((n2 & 0x7f) << 8) | b[q]; q += 1
            strs.append(b[q:q+n2].decode('utf-8', 'replace'))
        else:
            n = struct.unpack('<H', b[q:q+2])[0]; q += 2
            if n & 0x8000:
                n = ((n & 0x7fff) << 16) | struct.unpack('<H', b[q:q+2])[0]; q += 2
            strs.append(b[q:q+2*n].decode('utf-16le', 'replace'))
    return {'utf8': utf8, 'flags': flags, 'strings': strs, 'size': size, 'stycount': stycnt}

def build_pool(strings, utf8, flags, stycount):
    flags &= ~1  # clear sorted flag, order may change
    data = bytearray()
    offs = []
    for s in strings:
        offs.append(len(data))
        if utf8:
            enc = s.encode('utf-8')
            n = len(enc)
            if n > 0x7f:
                data += bytes([(n >> 8) | 0x80, n & 0xff])
            else:
                data += bytes([n])
            data += enc
            data.append(0)
        else:
            enc = s.encode('utf-16le')
            n = len(enc) // 2
            if n > 0x7fff:
                data += struct.pack('<HH', (n >> 16) | 0x8000, n & 0xffff)
            else:
                data += struct.pack('<H', n)
            data += enc + b'\x00\x00'
    if len(data) % 4:
        data += b'\x00' * (4 - len(data) % 4)
    header_size = 28
    strings_start = header_size + 4*len(strings) + 4*stycount
    chunk_size = strings_start + len(data)
    out = struct.pack('<HHI', TYPE_STRING_POOL, header_size, chunk_size)
    out += struct.pack('<IIIII', len(strings), stycount, flags, strings_start, 0)
    out += struct.pack('<%dI' % len(offs), *offs)
    out += b'\x00' * (4*stycount)
    out += bytes(data)
    return out

def patch_axml(b, replacements):
    typ, hsz, total = struct.unpack('<HHI', b[0:8])
    assert typ == TYPE_XML
    pool = parse_pool(b, 8)
    strings = [replacements.get(s, s) for s in pool['strings']]
    new_pool = build_pool(strings, pool['utf8'], pool['flags'], pool['stycount'])
    rest = b[8+pool['size']:]
    out = struct.pack('<HHI', TYPE_XML, hsz, 8 + len(new_pool) + len(rest)) + new_pool + rest
    return out

def main():
    apk_in, apk_out, old, new = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    zin = zipfile.ZipFile(apk_in)
    man = zin.read('AndroidManifest.xml')
    newman = patch_axml(man, {old: new})
    sig_suffix = ('.SF', '.RSA', '.DSA', '.EC')
    with zipfile.ZipFile(apk_out, 'w') as zout:
        for info in zin.infolist():
            name = info.filename
            if name.startswith('META-INF/') and name.upper().endswith(sig_suffix):
                continue
            if name == 'META-INF/MANIFEST.MF':
                continue
            data = newman if name == 'AndroidManifest.xml' else zin.read(name)
            zi = zipfile.ZipInfo(name, info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.internal_attr = info.internal_attr
            zi.create_system = info.create_system
            zout.writestr(zi, data)
    print('wrote', apk_out)

if __name__ == '__main__':
    main()
