"""Own-kill detection from decoded MapleStory server->client packets.

Opcodes (MapleStory Classic, identified on a live capture of 30 own kills; they can
shift when the game is patched, so they all live here):

  0x3C9 mob spawn          u8, u32 oid, u8 1, u32 mob type, ...  (some spawns carry
                           4 extra zero bytes before the oid)
  0x3D5 mob control        u8 mode, u32 oid [, u8 1, u32 mob type, ...]  (mode 2 = aggro)
  0x3F3 mob HP bar         u32 oid, u8 hp%  -- sent only to the player hitting the mob
  0x44D mob killed         u32 oid, u32 killer character id, u32 ?
  0x3D4 mob removed        u32 oid, u8 animation, ...  (every death on the map)

An own kill is a 0x44D whose killer is us. Our character id is learned from the
first mob whose HP bar we saw drop to 0% and whose 0x44D then names its killer.
Three signals agreed on the capture: 30 HP-bar-0 mobs, 30 0x44D with our id and
30 EXP updates.
"""
import struct

OP_SPAWN = 0x3C9
OP_CONTROL = 0x3D5
OP_HPBAR = 0x3F3
OP_KILLED = 0x44D


def _u32(b, i):
    return struct.unpack_from("<I", b, i)[0]


def _mob_type(b):
    """(oid, mob type) from a spawn/control packet, else None."""
    o = 3
    if len(b) >= o + 9 and b[o + 4] != 1 and len(b) >= o + 13 and b[o + 8] == 1:
        o = 7  # variant with 4 extra bytes before the oid
    if len(b) < o + 9 or b[o + 4] != 1:
        return None
    return _u32(b, o), _u32(b, o + 5)


class KillCounter:
    def __init__(self):
        self.self_id = None
        self.mob_type = {}  # oid -> mob type (oids are unique per channel session)
        self.hp_zero = set()  # oids whose HP bar we saw reach 0%
        self.counted = set()

    def feed(self, ts, p):
        """Feed one decoded packet; returns {'ts', 'oid', 'mob'} for an own kill, else None."""
        if len(p) < 2:
            return None
        op = p[0] | p[1] << 8
        if op == OP_SPAWN or (op == OP_CONTROL and len(p) > 3 and p[2] != 0):
            r = _mob_type(p)
            if r:
                self.mob_type[r[0]] = r[1]
        elif op == OP_HPBAR and len(p) >= 7:
            if p[6] == 0:
                self.hp_zero.add(_u32(p, 2))
        elif op == OP_KILLED and len(p) >= 10:
            oid, killer = _u32(p, 2), _u32(p, 6)
            if self.self_id is None and oid in self.hp_zero:
                self.self_id = killer
            if killer == self.self_id and oid not in self.counted:
                self.counted.add(oid)
                self.hp_zero.discard(oid)
                return {"ts": ts, "oid": oid, "mob": self.mob_type.pop(oid, None)}
            self.hp_zero.discard(oid)
            self.mob_type.pop(oid, None)
        return None
