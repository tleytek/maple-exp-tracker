"""Character state from decoded MapleStory server->client packets: EXP, level, mesos,
HP/MP and the Use-tab inventory (for potion use).

Packet layouts (MapleStory Classic, checked against the game HUD on a live capture:
level 45, EXP 216,944 = 46.31 %, MP 631/1208, max HP 1464):

  0x1A9 enter field / char info (sent on login, map and channel change)
        ... u32 id, u32 id, u32 ?, char[13] name, u8 gender, u8 skin, u32 face,
        u32 hair, u32 level, u16 job, u16 str, dex, int, luk, u32 hp, maxhp, mp, maxmp,
        u16 ap, u16 ?, u32 ?, u64 exp ...   (offsets from the name below)
        Use items further on: u16 slot, u8 2, u32 item id, u8 cash, u64 expiry,
        u32 -1, u8 ?, u16 quantity
  0x07E stat update: u8 item reaction (1 right after using an item), u8 ?, u8 ?,
        u32 mask, values in flag order:
        HP 0x400 u32, MP 0x1000 u32, EXP 0x10000 u64, mesos 0x40000 u64
  0x072 inventory ops: u8 ?, u8 ?, u8 count, u32 ?, then per op u8 mode, u8 tab, u16 slot:
        mode 1 = new quantity (u16), 2 = moved to slot (u16), 3 = removed,
        0 = added (item record follows; same layout as in char info)
"""
import struct

OP_CHAR_INFO = 0x1A9
OP_STATS = 0x07E
OP_INVENTORY = 0x072

USE_TAB = 2
F_HP, F_MAXHP, F_MP, F_MAXMP, F_EXP, F_MESO = 0x400, 0x800, 0x1000, 0x2000, 0x10000, 0x40000
# Value sizes of the flags seen so far. A stat packet with any other flag is only
# read up to that flag (sizes of e.g. level/AP on this server are not verified yet).
FLAG_SIZES = [(F_HP, 4), (F_MAXHP, 4), (F_MP, 4), (F_MAXMP, 4), (F_EXP, 8), (F_MESO, 8)]
KNOWN_MASK = sum(f for f, _ in FLAG_SIZES)

# Offsets from the start of the 13-byte name in the char info packet.
NAME_LEN, LEVEL_AT, JOB_AT, HP_AT, EXP_AT = 13, 27, 31, 41, 65


def _u16(b, i):
    return struct.unpack_from("<H", b, i)[0]


def _u32(b, i):
    return struct.unpack_from("<I", b, i)[0]


def _u64(b, i):
    return struct.unpack_from("<Q", b, i)[0]


def find_char(b, char_id=None):
    """Offset of our character's name in a char info packet (anchored on the id
    written twice right before it), else None."""
    for i in range(2, min(len(b) - 80, 2048)):
        cid = _u32(b, i)
        if cid == 0 or _u32(b, i + 4) != cid or (char_id is not None and cid != char_id):
            continue
        n = i + 12
        name = b[n:n + NAME_LEN].split(b"\0", 1)[0]
        if 2 <= len(name) <= 12 and all(48 <= c < 123 for c in name) and 1 <= _u32(b, n + LEVEL_AT) <= 250:
            return n
    return None


def use_items(b, start=0):
    """{slot: (item id, quantity)} for Use-tab item records in a packet."""
    items = {}
    for j in range(max(start, 3), len(b) - 20):
        if b[j - 1] != 2 or b[j + 13:j + 17] != b"\xff\xff\xff\xff":
            continue
        iid = _u32(b, j)
        if 2000000 <= iid < 3000000:
            items[_u16(b, j - 3)] = (iid, _u16(b, j + 18))
    return items


class PlayerState:
    """Feed decoded packets; keeps the latest values and returns change events.

    Events: ('exp', old, new, level), ('level', old, new), ('meso', old, new),
            ('use', item id, old total, new total),
            ('pot_kind', item id, 'hp' | 'mp', total before the use) the first time a
            used item turns out to heal HP or MP (the item-reaction stat update that
            follows a use says which).

    to_next(level) -> EXP for a full bar; with it, an EXP drop of more than 30 % of
    the bar between char info packets is taken as a level-up (the level-up stat
    packet itself has flags whose sizes aren't known yet).
    """

    def __init__(self, to_next=None, item_kind=None):
        self.to_next = to_next
        self.item_kind = dict(item_kind or {})  # item id -> 'hp' / 'mp'
        self._pending_use = {}  # item id -> total before the latest use
        self.char_id = None
        self.name = None
        self.level = None
        self.job = None
        self.exp = None
        self.mesos = None
        self.hp = self.max_hp = self.mp = self.max_mp = None
        self.use = {}  # Use tab slot -> [item id, quantity]
        self.use_known = False  # Use tab loaded from a char info packet

    def totals(self):
        t = {}
        for iid, q in self.use.values():
            t[iid] = t.get(iid, 0) + q
        return t

    def feed(self, p):
        if len(p) < 2:
            return []
        op = p[0] | p[1] << 8
        if op == OP_CHAR_INFO and len(p) > 1000:
            return self._char_info(p)
        if op == OP_STATS and len(p) >= 9:
            return self._stats(p)
        if op == OP_INVENTORY and len(p) >= 9:
            return self._inventory(p)
        return []

    def _char_info(self, b):
        n = find_char(b, self.char_id)
        if n is None:
            return []
        ev = []
        self.char_id = _u32(b, n - 12)
        self.name = b[n:n + NAME_LEN].split(b"\0", 1)[0].decode("ascii", "replace")
        level = _u32(b, n + LEVEL_AT)
        if self.level is not None and level != self.level:
            ev.append(("level", self.level, level))
        self.level = level
        self.job = _u16(b, n + JOB_AT)
        self.hp, self.max_hp, self.mp, self.max_mp = struct.unpack_from("<4I", b, n + HP_AT)
        exp = _u64(b, n + EXP_AT)
        if self.exp is not None and exp != self.exp:
            ev.append(("exp", self.exp, exp, level))
        self.exp = exp
        before = self.totals()
        self.use = {s: [iid, q] for s, (iid, q) in use_items(b, n + EXP_AT).items()}
        if self.use_known:
            ev += self._use_events(before)
        self.use_known = True
        return ev

    def _stats(self, b):
        mask = _u32(b, 5)
        i = 9
        vals = {}
        for flag, size in FLAG_SIZES:
            if mask & ~KNOWN_MASK & (flag - 1):
                break  # an unknown flag comes first: can't place the rest
            if mask & flag:
                if i + size > len(b):
                    break
                vals[flag] = _u32(b, i) if size == 4 else _u64(b, i)
                i += size
        ev = []
        if F_HP in vals:
            self.hp = vals[F_HP]
        if F_MAXHP in vals:
            self.max_hp = vals[F_MAXHP]
        if F_MP in vals:
            self.mp = vals[F_MP]
        if F_MAXMP in vals:
            self.max_mp = vals[F_MAXMP]
        if b[2] == 1 and self._pending_use:  # stat change caused by the item just used
            kind = "hp" if F_HP in vals else "mp" if F_MP in vals else None
            if kind:
                for iid, before in self._pending_use.items():
                    if iid not in self.item_kind:
                        self.item_kind[iid] = kind
                        ev.append(("pot_kind", iid, kind, before))
            self._pending_use = {}
        if F_EXP in vals:
            full = self.to_next(self.level) if self.to_next and self.level else None
            if self.exp is not None and full and self.exp - vals[F_EXP] > 0.3 * full:
                ev.append(("level", self.level, self.level + 1))
                self.level += 1
            if self.exp is not None and vals[F_EXP] != self.exp:
                ev.append(("exp", self.exp, vals[F_EXP], self.level))
            self.exp = vals[F_EXP]
        if F_MESO in vals:
            if self.mesos is not None and vals[F_MESO] != self.mesos:
                ev.append(("meso", self.mesos, vals[F_MESO]))
            self.mesos = vals[F_MESO]
        return ev

    def _inventory(self, b):
        count = b[4]
        before = self.totals()
        i = 9
        for _ in range(count):
            if i + 4 > len(b):
                break
            mode, tab, slot = b[i], b[i + 1], _u16(b, i + 2)
            i += 4
            if mode == 1:  # new quantity
                q = _u16(b, i)
                i += 2
                if tab == USE_TAB and slot in self.use:
                    self.use[slot][1] = q
            elif mode == 2:  # move / swap
                dst = _u16(b, i)
                i += 2
                if tab == USE_TAB:
                    a, c = self.use.pop(slot, None), self.use.pop(dst, None)
                    if a:
                        self.use[dst] = a
                    if c:
                        self.use[slot] = c
            elif mode == 3:  # removed
                if tab == USE_TAB:
                    self.use.pop(slot, None)
            elif mode == 0:  # added: item record follows (type byte, then id)
                if tab == USE_TAB and i + 21 <= len(b) and b[i] == 2:
                    self.use[slot] = [_u32(b, i + 1), _u16(b, i + 19)]
                break  # record length varies; stop reading further ops
            else:
                break
        if not self.use_known:
            return []
        ev = self._use_events(before)
        used = {e[1]: e[2] for e in ev if e[3] < e[2]}
        if used:
            self._pending_use = used
        return ev

    def _use_events(self, before):
        after = self.totals()
        return [("use", iid, before.get(iid, 0), after.get(iid, 0))
                for iid in sorted(set(before) | set(after)) if before.get(iid, 0) != after.get(iid, 0)]
