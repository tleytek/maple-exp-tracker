import json
import os
import unittest

from census import LiveLines, MapCensus, field_map_id, user_enter, format_summary

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "tests", "census", "burnt_land_capture.jsonl")


def replay(path):
    c, out = MapCensus(), []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            s = c.feed(r["ts"], bytes.fromhex(r["hex"]))
            if s:
                out.append(s)
    s = c.finish()
    if s:
        out.append(s)
    return out


class CensusCaptureTest(unittest.TestCase):
    """A live 2-minute capture: The Burnt Land IV with a party, then The Burnt Land V."""

    @classmethod
    def setUpClass(cls):
        cls.visits = replay(FIXTURE)

    def test_two_map_visits(self):
        self.assertEqual([v["map"] for v in self.visits], [10004114, 10004115])

    def test_refills_top_up_to_the_cap(self):
        iv, v = self.visits
        self.assertEqual(iv["peak_alive"], 15)
        self.assertEqual(iv["alive_after_refill"], {"15": 10})
        self.assertEqual(v["peak_alive"], 17)
        self.assertEqual(v["alive_after_refill"], {"17": 3})

    def test_players_on_the_map(self):
        iv, v = self.visits
        self.assertEqual(iv["peak_alive_by_players"], {"5": 15})
        self.assertEqual(list(iv["players_seconds"]), ["5"])
        self.assertEqual(v["peak_alive_by_players"], {"8": 17})

    def test_refill_interval(self):
        gaps = self.visits[0]["refill_gaps"]
        self.assertEqual(len(gaps), 9)
        self.assertTrue(all(7.0 <= g <= 7.3 for g in gaps), gaps)

    def test_summary_line(self):
        line = format_summary(self.visits[0], {10004114: "The Burnt Land IV"})
        self.assertIn("LEFT map 10004114 The Burnt Land IV", line)
        self.assertIn("highest alive 15 (5 players: 15)", line)

    def test_spawn_positions_recorded(self):
        snaps = self.visits[0]["refill_snapshots"]
        self.assertEqual(len(snaps), 10)
        self.assertTrue(all(s["spawned"] for s in snaps))
        self.assertTrue(all(len(s["spawned"]) <= len(s["positions"]) <= s["alive"] for s in snaps))

    def test_live_lines_name_the_map_you_are_on(self):
        c, live, out = MapCensus(), LiveLines({10004114: "The Burnt Land IV", 10004115: "The Burnt Land V"}), []
        with open(FIXTURE, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                c.feed(r["ts"], bytes.fromhex(r["hex"]))
                out += live(c, r["ts"])
        self.assertEqual(out[0], "ENTERED map 10004114 The Burnt Land IV")
        self.assertRegex(out[1], r"^  already there: \d+ alive \| 5 player\(s\) on the map$")
        refills = [x for x in out if "refill +" in x]
        self.assertTrue(refills and all("-> 15 alive" in x or "-> 17 alive" in x for x in refills), refills)
        self.assertIn("ENTERED map 10004115 The Burnt Land V", out)


class CensusPacketTest(unittest.TestCase):
    def test_short_set_field(self):
        b = bytearray(103)
        b[0:2] = b"\xa9\x01"
        b[36:40] = (10002081).to_bytes(4, "little")
        self.assertEqual(field_map_id(bytes(b)), 10002081)

    def test_user_enter(self):
        b = bytes.fromhex("2d02ac89950b11b70100000000002900000008004b656e64696b6f730000")
        self.assertEqual(user_enter(b), (0x1B711, 41, "Kendikos"))
        b = bytes.fromhex("2d02956b7804000000002bc00100000000002900000004006e6561610000")
        self.assertEqual(user_enter(b), (0x1C02B, 41, "neaa"))

    def test_leave_and_rejoin(self):
        c = MapCensus()
        b = bytearray(103)
        b[0:2] = b"\xa9\x01"
        b[36:40] = (1).to_bytes(4, "little")
        c.feed(0.0, bytes(b))
        enter = bytes.fromhex("2d02ac89950b11b70100000000002900000008004b656e64696b6f730000")
        c.feed(0.1, enter)
        self.assertEqual(c.visit.players, 2)
        c.feed(5.0, bytes.fromhex("2e0211b70100"))
        self.assertEqual(c.visit.players, 1)


if __name__ == "__main__":
    unittest.main()
