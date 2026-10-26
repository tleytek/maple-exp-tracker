"""Passive MapleStory (Classic) server->client packet decoder.

Copied from the maple-packet-watch project (maple_sniff.py + fb.py), where the
protocol was verified on live captures. Read-only: packets are only observed
through tshark, never injected, replayed or modified.

  * header: 4 bytes. iiv = (b0 << 8 | b1) = (iv[2] << 8 | iv[3]) ^ 0xFEFF
            length = (b0|b1<<8) ^ (b2|b3<<8)
  * body (server->client): plain = (cipher - iv[0]) mod 256, per packet
  * the IV advances once per packet with the standard Maple getNewIv shuffle.
Client->server bodies use a stronger cipher and are not decoded.
"""
import os
import subprocess
import sys

TSHARK = r"C:\Program Files\Wireshark\tshark.exe"
HDR_XOR = 0xFEFF
DEFAULT_FILTER = "tcp and not port 443 and not port 80"
SYNC_CHAIN = 4  # consecutive complete packets needed to accept an IV

# getNewIv shuffle table ("funnyBytes").
FB = bytes([236, 63, 119, 164, 69, 208, 113, 191, 183, 152, 32, 252, 75, 233, 179, 225, 92, 34, 247, 12, 68, 27, 129, 189, 99, 141, 212, 195, 242, 16, 25, 224, 251, 161, 110, 102, 234, 174, 214, 206, 6, 24, 78, 235, 120, 149, 219, 186, 182, 66, 122, 42, 131, 11, 84, 103, 109, 232, 101, 231, 47, 7, 243, 170, 39, 123, 133, 176, 38, 253, 139, 169, 250, 190, 168, 215, 203, 204, 146, 218, 249, 147, 96, 45, 221, 210, 162, 155, 57, 95, 130, 33, 76, 105, 248, 49, 135, 238, 142, 173, 140, 106, 188, 181, 107, 89, 19, 241, 4, 0, 246, 90, 53, 121, 72, 143, 21, 205, 151, 87, 18, 62, 55, 255, 157, 79, 81, 245, 163, 112, 187, 20, 117, 194, 184, 114, 192, 237, 125, 104, 201, 46, 13, 98, 70, 23, 17, 77, 108, 196, 126, 83, 193, 37, 199, 154, 28, 136, 88, 44, 137, 220, 2, 100, 64, 1, 93, 56, 165, 226, 175, 85, 213, 239, 26, 124, 167, 91, 166, 111, 134, 159, 115, 230, 10, 222, 43, 153, 74, 71, 156, 223, 9, 118, 158, 48, 14, 228, 178, 148, 160, 59, 52, 29, 40, 15, 54, 227, 35, 180, 3, 216, 144, 200, 60, 254, 94, 50, 36, 80, 31, 58, 67, 138, 150, 65, 116, 172, 82, 51, 240, 217, 41, 128, 177, 22, 211, 171, 145, 185, 132, 127, 97, 30, 207, 197, 209, 86, 61, 202, 244, 5, 198, 229, 8, 73])


def new_iv(iv):
    s = [0xF2, 0x53, 0x50, 0xC6]
    for inp in iv:
        m = (FB[s[1]] - inp) & 0xFF
        s[0] = (s[0] + m) & 0xFF
        m = s[2] ^ FB[inp]
        s[1] = (s[1] - m) & 0xFF
        e = (s[3] - s[0]) & 0xFF
        s[2] = ((FB[s[3]] + inp) & 0xFF) ^ s[2]
        s[3] = (e + FB[inp]) & 0xFF
        v = s[0] | s[1] << 8 | s[2] << 16 | s[3] << 24
        v = ((v << 3) | (v >> 29)) & 0xFFFFFFFF
        s = [v & 0xFF, v >> 8 & 0xFF, v >> 16 & 0xFF, v >> 24 & 0xFF]
    return s


def hdr(buf, i):
    w0 = buf[i] | buf[i + 1] << 8
    w1 = buf[i + 2] | buf[i + 3] << 8
    return (buf[i] << 8 | buf[i + 1]), w0 ^ w1


def length_chain(buf, off, count=SYNC_CHAIN):
    """Header tags of `count` back-to-back complete packets starting at off, else None."""
    tags = []
    for _ in range(count):
        if off + 4 > len(buf):
            return None
        iiv, n = hdr(buf, off)
        if n < 2 or off + 4 + n > len(buf):
            return None
        tags.append(iiv ^ HDR_XOR)
        off += 4 + n
    return tags


def sync(buf, max_scan=4096):
    """Find (offset, iv) where SYNC_CHAIN consecutive headers chain under getNewIv."""
    for off in range(min(max_scan, len(buf) - 4)):
        tags = length_chain(buf, off)
        if tags is None:
            continue
        t0 = tags[0]
        for a in range(256):
            for b in range(256):
                iv = [a, b, t0 >> 8, t0 & 0xFF]
                nx = iv
                ok = True
                for t in tags[1:]:
                    nx = new_iv(nx)
                    if (nx[2] << 8 | nx[3]) != t:
                        ok = False
                        break
                if ok:
                    return off, iv
    return None


class Stream:
    """One server->client TCP stream: reassembly, IV sync and decryption."""

    def __init__(self, key, log=None):
        self.key = key
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.buf = bytearray()
        self.next_seq = None
        self.iv = None
        self.pos = 0
        self.dead = False
        self.strikes = 0

    def feed(self, seq, data):
        if self.next_seq is not None:
            if seq + len(data) <= self.next_seq:
                return  # retransmit
            if seq < self.next_seq:
                data = data[self.next_seq - seq:]
                seq = self.next_seq
            elif seq > self.next_seq:  # gap: lost data, must resync
                self.log(f"[{self.key}] gap {seq - self.next_seq}B, resync")
                self.buf = bytearray()
                self.pos = 0
                self.iv = None
        self.buf += data
        self.next_seq = seq + len(data)

    def packets(self):
        if self.dead:
            return
        if self.iv is None:
            if len(self.buf) < 64:
                return
            r = sync(self.buf)
            if r is None:
                # A fresh connection (channel change) arrives a few packets at a time, so
                # only give up after a lot of unsyncable data, and retry before ignoring it.
                if len(self.buf) > 16384:
                    self.strikes += 1
                    self.buf = bytearray()
                    self.pos = 0
                    if self.strikes >= 3:
                        self.dead = True
                        self.log(f"[{self.key}] not a Maple server stream, ignoring")
                return
            self.pos, self.iv = r
            self.log(f"[{self.key}] synced")
        while self.pos + 4 <= len(self.buf):
            iiv, n = hdr(self.buf, self.pos)
            if iiv ^ HDR_XOR != (self.iv[2] << 8 | self.iv[3]):
                self.log(f"[{self.key}] header check failed, resync")
                del self.buf[: self.pos]
                self.pos = 0
                self.iv = None
                return
            if self.pos + 4 + n > len(self.buf):
                break
            body = self.buf[self.pos + 4 : self.pos + 4 + n]
            k = self.iv[0]
            yield bytes((c - k) & 0xFF for c in body)
            self.pos += 4 + n
            self.iv = new_iv(self.iv)
        if self.pos > 65536:
            del self.buf[: self.pos]
            self.pos = 0


def tshark_cmd(pcap=None, iface=None, bpf=DEFAULT_FILTER, duration=None):
    src = ["-r", pcap] if pcap else ["-i", iface, "-l", "-f", bpf]
    if duration and not pcap:
        src += ["-a", f"duration:{duration}"]
    return [TSHARK, *src, "-Y", "tcp.len>0", "-T", "fields", "-E", "separator=\t",
            "-e", "frame.time_epoch", "-e", "ip.src", "-e", "tcp.srcport", "-e", "ip.dst",
            "-e", "tcp.dstport", "-e", "tcp.seq_raw", "-e", "data"]


def decode_lines(lines, log=None):
    """Yield (ts, stream_key, plaintext) for every decoded server->client packet."""
    streams = {}
    for line in lines:
        f = line.rstrip("\r\n").split("\t")
        if len(f) < 7 or not f[6]:
            continue
        ts, sip, sport, dip, dport, seq, data = f
        if int(sport) >= 49152:  # ephemeral source port = our client -> server; not decodable
            continue
        key = f"{sip}:{sport}->{dip}:{dport}"
        st = streams.get(key) or streams.setdefault(key, Stream(key, log))
        st.feed(int(seq), bytes.fromhex(data))
        for p in st.packets():
            yield float(ts), key, p


def open_tshark(**kw):
    """Start tshark; returns the Popen (stdout = text lines for decode_lines)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash from the windowed exe
    return subprocess.Popen(tshark_cmd(**kw), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1, creationflags=flags)


def tshark_available():
    return os.path.exists(TSHARK)


def opcode(p):
    return p[0] | p[1] << 8 if len(p) >= 2 else -1
