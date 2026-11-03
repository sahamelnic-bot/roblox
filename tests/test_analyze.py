import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import common as c
from tracker import main as tmain
from tracker import roblox, steam, twitch, youtube

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
H = lambda h: NOW - timedelta(hours=h)  # noqa: E731


def rb(uid, name, playing, visits, created="2026-10-25T00:00:00Z", updated="2026-10-26T00:00:00Z"):
    return {"universe_id": uid, "name": name, "playing": playing, "visits": visits, "favorites": 0,
            "up": 90, "down": 10, "created": created, "updated": updated, "genre": "",
            "root_place_id": str(uid * 10), "sorts": "top-trending"}


OLD = "2020-01-01T00:00:00Z"


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class MomentumTest(Tmp):
    def test_same_time_yesterday_and_streak(self):
        rows = [{"ts": H(72), "v": "100"}, {"ts": H(48), "v": "200"}, {"ts": H(24), "v": "400"},
                {"ts": H(6), "v": "500"}, {"ts": NOW, "v": "2000"}]
        m = c.momentum("k", rows, "v", NOW)
        self.assertAlmostEqual(m.g24, 4.0)
        self.assertAlmostEqual(m.x24, 5.0)
        self.assertAlmostEqual(m.g_prev24, 1.0)
        self.assertAlmostEqual(m.accel, 3.0)
        self.assertEqual(m.streak, 3)
        self.assertAlmostEqual(m.g6, 3.0)

    def test_stale_game_dropped(self):
        rows = [{"ts": H(10), "v": "100"}]
        self.assertIsNone(c.momentum("k", rows, "v", NOW))

    def test_market_relative_is_multiplicative(self):
        a = c.Momentum("a", "a", 1000, g24=1.0)   # x2
        b = c.Momentum("b", "b", 1000, g24=0.0)
        d = c.Momentum("d", "d", 1000, g24=0.0)
        med = c.apply_market([a, b, d], 300)
        self.assertEqual(med["g24"], 0.0)
        self.assertAlmostEqual(a.rel24, 1.0)


class RobloxTest(Tmp):
    def setUp(self):
        super().setUp()
        pts = {
            H(48): [rb(1, "Steal a Cat", 1000, 10_000), rb(2, "Escape Tsunami For Cats", 1000, 10_000),
                    rb(3, "Old Obby", 10_000, 1_000_000, OLD, OLD), rb(4, "Tsunami Run [UPD]", 500, 5_000),
                    rb(5, "Mega Tycoon", 2000, 50_000, OLD, OLD)],
            H(24): [rb(1, "Steal a Cat", 1000, 20_000), rb(2, "Escape Tsunami For Cats", 1000, 20_000),
                    rb(3, "Old Obby", 10_000, 1_100_000, OLD, OLD), rb(4, "Tsunami Run [UPD]", 500, 6_000),
                    rb(5, "Mega Tycoon", 2000, 60_000, OLD, OLD)],
            H(6): [rb(1, "Steal a Cat", 1100, 25_000), rb(2, "Escape Tsunami For Cats", 1500, 30_000),
                   rb(3, "Old Obby", 9_000, 1_150_000, OLD, OLD), rb(4, "Tsunami Run [UPD]", 600, 6_500),
                   rb(5, "Mega Tycoon", 4000, 70_000, OLD, OLD)],
            NOW: [rb(1, "Steal a Cat", 1100, 30_000), rb(2, "Escape Tsunami For Cats", 3000, 60_000),
                  rb(3, "Old Obby", 10_000, 1_200_000, OLD, OLD), rb(4, "Tsunami Run [UPD]", 1000, 7_500),
                  rb(5, "Mega Tycoon", 8000, 90_000, OLD, "2026-11-02T00:00:00Z")],  # апдейт старої гри
        }
        for ts, rows in pts.items():
            c.append_csv(self.dir, ts, rows, roblox.FIELDS)
        self.games, _ = roblox.compute(self.dir, NOW, 300)
        self.by = {int(g.m.key): g for g in self.games}

    def test_metrics(self):
        g = self.by[2]
        self.assertAlmostEqual(g.m.x24, 3.0)
        self.assertEqual(g.visits_24h, 40_000)
        self.assertAlmostEqual(g.session_min, 3000 * 1440 / 40_000)
        self.assertAlmostEqual(g.like, 0.9)

    def test_update_spike_flagged_and_not_a_rocket(self):
        self.assertTrue(self.by[5].update_spike)
        sec = roblox.analyze(self.dir, NOW, 300)
        rockets_part = sec.md.split("### ⚡")[0]
        self.assertNotIn("Mega Tycoon", rockets_part)
        self.assertIn("Сплески від оновлень", sec.md)

    def test_rocket_order(self):
        sec = roblox.analyze(self.dir, NOW, 300)
        self.assertIn("Escape Tsunami For Cats", sec.tg[1])

    def test_topics_and_clones(self):
        t = {x.word: x for x in roblox.topics(self.games, min_total=0)}
        self.assertEqual(t["tsunami"].games, 2)
        self.assertEqual(t["tsunami"].clones_14d, 2)
        self.assertAlmostEqual(t["tsunami"].growth, (4000 - 1500) / 1500)
        self.assertNotIn("upd", t)

    def test_extract_nested(self):
        payload = {"sorts": [{"games": [{"universeId": 1}]}, {"x": {"items": [{"universeId": "2"}]}}]}
        self.assertEqual({int(g["universeId"]) for g in roblox.extract_games(payload)}, {1, 2})


class SteamTest(Tmp):
    def test_analyze(self):
        meta = self.dir / "apps.json"
        meta.write_text(json.dumps({"10": {"name": "Paint Hide", "release": "20 Oct, 2026", "price": "$5.99"},
                                    "20": {"name": "Big Shooter", "release": "1 Jan, 2015"}}))
        for ts, a, b in ((H(24), 300, 100_000), (NOW, 3000, 101_000)):
            c.append_csv(self.dir / "s", ts, [{"appid": 10, "name": "Paint Hide", "players": a},
                                              {"appid": 20, "name": "Big Shooter", "players": b}], steam.FIELDS)
        sec = steam.analyze(self.dir / "s", meta, NOW)
        self.assertIn("Paint Hide", sec.tg[1])
        self.assertIn("x10.0", sec.tg[1])
        self.assertIn("14 дн", sec.md)  # вік від релізу
        self.assertIn("paint", sec.rising)


class TwitchTest(Tmp):
    def test_analyze(self):
        for ts, v in ((H(24), 600), (NOW, 6000)):
            c.append_csv(self.dir, ts, [{"game_id": "1", "name": "Paint Hide", "viewers": v, "channels": 30},
                                        {"game_id": "2", "name": "Big Shooter", "viewers": 90_000, "channels": 900}],
                         twitch.FIELDS)
        sec = twitch.analyze(self.dir, NOW)
        self.assertIn("Paint Hide", sec.tg[1])


class YoutubeTest(Tmp):
    def test_hits_and_topics(self):
        rows = []
        for ch in ("A", "B"):
            for i, d in enumerate((5, 7, 9)):
                rows.append({"video_id": f"{ch}{i}", "channel_id": ch, "channel": ch, "title": f"Obby run {i}",
                             "published": c.fmt_ts(NOW - timedelta(days=d)), "views": 100_000})
            rows.append({"video_id": f"{ch}new", "channel_id": ch, "channel": ch,
                         "title": "Toothy Cat is EVERYWHERE", "published": c.fmt_ts(H(20)),
                         "views": 250_000 if ch == "A" else 50_000})
        c.append_csv(self.dir, NOW, rows, youtube.FIELDS)
        sec = youtube.analyze(self.dir, NOW)
        self.assertIn("250%", sec.md)
        self.assertIn("toothy", sec.rising)
        self.assertNotIn("obby", sec.rising)

    def test_read_handles(self):
        p = self.dir / "c.txt"
        p.write_text("# x\n@KreekCraft\nFlamingo # c\nUCabc\n\n")
        self.assertEqual(youtube.read_handles(p), ["@KreekCraft", "@Flamingo", "UCabc"])


class CrossoverTest(unittest.TestCase):
    def test_crossover_needs_two_sources(self):
        from tracker.report import Section
        secs = [Section("roblox", "R", rising={"toothy": "R1", "obby": "R2"}),
                Section("youtube", "Y", rising={"toothy": "Y1"})]
        cross = tmain.crossovers(secs)
        self.assertEqual([w for w, _ in cross], ["toothy"])
        md, tg = tmain.render(NOW, secs, {"steam": "boom"}, {"twitch": "немає ключів"})
        self.assertIn("toothy", tg)
        self.assertIn("❌ **steam**", md)
        self.assertIn("⏸ **twitch**", md)


if __name__ == "__main__":
    unittest.main()


class RobloxResilienceTest(Tmp):
    def test_partial_429_keeps_other_chunks(self):
        from unittest import mock
        import urllib.error
        calls = {"n": 0}

        def fake(url, params=None, **kw):
            if url == roblox.GAMES:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise urllib.error.HTTPError(url, 429, "Too Many Requests", None, None)
                ids = params["universeIds"].split(",")
                return {"data": [{"id": int(i), "name": f"G{i}", "playing": 500} for i in ids]}
            return {"data": []}

        with mock.patch.object(c, "http_json", side_effect=fake):
            out = roblox.details(range(1, 101))   # 2 пачки по 50, перша падає
        self.assertEqual(len(out), 50)

    def test_all_chunks_fail_raises(self):
        from unittest import mock
        with mock.patch.object(c, "http_json", side_effect=RuntimeError("429")):
            with self.assertRaises(RuntimeError):
                roblox.details([1, 2, 3])

    def test_home_down_uses_history(self):
        from unittest import mock
        c.append_csv(self.dir, H(1), [rb(7, "Known Game", 900, 10)], roblox.FIELDS)
        with mock.patch.object(roblox, "discover", side_effect=RuntimeError("429")), \
             mock.patch.object(roblox, "details", return_value={7: {"name": "Known Game", "playing": 950}}) as d:
            msg = roblox.collect(self.dir, self.dir / "none.txt", NOW)
        self.assertEqual(set(d.call_args[0][0]), {7})
        self.assertIn("записано 1", msg)
