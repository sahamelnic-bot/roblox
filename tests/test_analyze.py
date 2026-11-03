import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tracker import analyze, roblox_api, storage

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def row(uid, name, playing, visits, created="2026-10-25T00:00:00Z", place="1"):
    return {"universe_id": uid, "name": name, "playing": playing, "visits": visits,
            "favorites": 0, "up": 90, "down": 10, "created": created, "updated": "",
            "genre": "", "root_place_id": place, "sorts": "top-trending"}


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # 48 год тому, 24 год тому, 6 год тому, зараз
        points = {
            NOW - timedelta(hours=48): [row(1, "Steal a Cat", 1000, 10_000), row(2, "Escape Tsunami For Cats", 1000, 10_000),
                                        row(3, "Old Obby", 10_000, 1_000_000, created="2020-01-01T00:00:00Z"),
                                        row(4, "Tsunami Run [UPD]", 500, 5_000)],
            NOW - timedelta(hours=24): [row(1, "Steal a Cat", 1000, 20_000), row(2, "Escape Tsunami For Cats", 1000, 20_000),
                                        row(3, "Old Obby", 10_000, 1_100_000, created="2020-01-01T00:00:00Z"),
                                        row(4, "Tsunami Run [UPD]", 500, 6_000)],
            NOW - timedelta(hours=6): [row(1, "Steal a Cat", 1100, 25_000), row(2, "Escape Tsunami For Cats", 1500, 30_000),
                                       row(3, "Old Obby", 9_000, 1_150_000, created="2020-01-01T00:00:00Z"),
                                       row(4, "Tsunami Run [UPD]", 600, 6_500)],
            NOW: [row(1, "Steal a Cat", 1100, 30_000), row(2, "Escape Tsunami For Cats", 3000, 60_000),
                  row(3, "Old Obby", 10_000, 1_200_000, created="2020-01-01T00:00:00Z"),
                  row(4, "Tsunami Run [UPD]", 1000, 7_500)],
        }
        for ts, rows in points.items():
            storage.append(self.dir, ts, rows)
        self.cfg = analyze.Cfg(min_ccu=300)
        self.stats = analyze.compute(storage.load(self.dir, NOW, days=3), now=NOW, cfg=self.cfg)
        self.by_id = {s.universe_id: s for s in self.stats}

    def tearDown(self):
        self.tmp.cleanup()

    def test_growth_24h(self):
        self.assertAlmostEqual(self.by_id[2].g24, 2.0)    # 1000 -> 3000
        self.assertAlmostEqual(self.by_id[1].g24, 0.1)
        self.assertAlmostEqual(self.by_id[3].g24, 0.0)

    def test_rockets_sorted_by_relative_growth(self):
        rockets = analyze.rank(self.stats, self.cfg)["rockets"]
        self.assertEqual(rockets[0].universe_id, 2)
        # медіана [0.1, 2.0, 0.0, 1.0] = 0.55
        self.assertAlmostEqual(rockets[0].rel24, 2.0 - 0.55)

    def test_visits_acceleration(self):
        s = self.by_id[2]
        self.assertEqual(s.visits_24h, 40_000)
        self.assertEqual(s.visits_prev_24h, 10_000)
        self.assertAlmostEqual(s.accel, 3.0)

    def test_fresh_excludes_old_games(self):
        fresh_ids = {s.universe_id for s in analyze.rank(self.stats, self.cfg)["fresh"]}
        self.assertNotIn(3, fresh_ids)
        self.assertIn(2, fresh_ids)

    def test_keyword_trend_groups_games(self):
        trends = {t.token: t for t in analyze.keyword_trends(self.stats, self.cfg, min_playing=0)}
        self.assertIn("tsunami", trends)
        self.assertEqual(trends["tsunami"].games, 2)
        self.assertAlmostEqual(trends["tsunami"].growth, (4000 - 1500) / 1500)
        self.assertNotIn("upd", trends)  # [UPD] вирізається

    def test_report_renders(self):
        md = analyze.render_markdown(self.stats, self.cfg, NOW)
        self.assertIn("Escape Tsunami For Cats", md)
        tg = analyze.render_telegram(self.stats, self.cfg, NOW)
        self.assertIn("🚀", tg)

    def test_no_history_is_ok(self):
        d = Path(self.tmp.name) / "empty"
        storage.append(d, NOW, [row(1, "A", 500, 1)])
        stats = analyze.compute(storage.load(d, NOW), now=NOW, cfg=self.cfg)
        self.assertIsNone(stats[0].g24)
        self.assertIn("зачекай добу", analyze.render_markdown(stats, self.cfg, NOW))


class ExtractTest(unittest.TestCase):
    def test_extract_nested(self):
        payload = {"sorts": [{"sortId": "x", "games": [{"universeId": 1, "name": "a"}]},
                             {"sortId": "y", "contents": {"items": [{"universeId": "2"}]}}]}
        self.assertEqual({int(g["universeId"]) for g in roblox_api.extract_games(payload)}, {1, 2})


if __name__ == "__main__":
    unittest.main()
