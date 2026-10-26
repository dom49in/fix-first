import unittest
from datetime import date

from app import scoring


class TestScoring(unittest.TestCase):
    def test_working_days_skip_weekends_and_holidays(self):
        # Thu 3 Sep 2026 -> Tue 8 Sep: Fri 4, Mon 7 (Labor Day), Tue 8
        self.assertEqual(scoring.working_days(date(2026, 9, 3), date(2026, 9, 8), set()), 3)
        self.assertEqual(scoring.working_days(date(2026, 9, 3), date(2026, 9, 8), {date(2026, 9, 7)}), 2)

    def test_raising_day_does_not_count_and_same_day_is_zero(self):
        self.assertEqual(scoring.working_days(date(2026, 9, 8), date(2026, 9, 8), set()), 0)

    def test_weather_thresholds(self):
        rows = [{"weather_date": "2026-09-01", "precip_mm": 12, "wind_max_kmh": 10, "temp_max_c": 25, "temp_min_c": 15},
                {"weather_date": "2026-09-02", "precip_mm": 1, "wind_max_kmh": 60, "temp_max_c": 36, "temp_min_c": 15},
                {"weather_date": "2026-09-03", "precip_mm": 1, "wind_max_kmh": 10, "temp_max_c": 25, "temp_min_c": 15}]
        bad = scoring.weather_bad_days(rows)
        self.assertEqual(sorted(bad), [date(2026, 9, 1), date(2026, 9, 2)])
        self.assertIn("heavy rain", bad[date(2026, 9, 1)])
        self.assertIn("high wind", bad[date(2026, 9, 2)])

    def test_score_and_overdue_with_weather_allowance(self):
        row = {"work_order_id": 1.0, "initiated_date": "2026/09/01 10:00:00+00", "priority_level": 4, "target_days": 3,
               "weight": 3, "asset_group": "TOA"}
        bad = {date(2026, 9, 2): "heavy rain", date(2026, 9, 3): "heavy rain"}
        o = scoring.score_order(row, date(2026, 9, 8), set(), bad)      # Tue 2 .. Tue 8 = 5 working days
        self.assertEqual(o["age_working_days"], 5)
        self.assertTrue(o["overdue"])
        self.assertEqual(o["weather_days"], 2)
        self.assertFalse(o["overdue_net_of_weather"])        # 5 - 2 = 3, not > 3
        self.assertEqual(o["score"], round(3 * (1 + 5 / 3), 2))
        self.assertEqual(o["work_order_id"], 1)

    def test_missing_policy_uses_default_and_flag(self):
        o = scoring.score_order({"initiated_date": "2026/09/08", "priority_level": 0, "weight": 2}, date(2026, 9, 8), set(), {})
        self.assertTrue(o["policy_missing"] and o["unprioritised"])
        self.assertEqual(o["target_days"], scoring.DEFAULT_TARGET_DAYS)

    def test_matches_filters(self):
        o = {"ward": "7", "priority_level": 0, "asset_group": "TOA", "category": None}
        self.assertTrue(scoring.matches(o, [{"dimension": "ward", "member": "7"}, {"dimension": "priority_level", "member": "0"}]))
        self.assertTrue(scoring.matches(o, [{"dimension": "category", "member": "(none)"}]))
        self.assertFalse(scoring.matches(o, [{"dimension": "ward", "member": "2"}]))


if __name__ == "__main__":
    unittest.main()


class TestHistory(unittest.TestCase):
    def rows(self):
        out = []
        for i in range(30):      # 30 quick closures: 1 working day (Mon -> Tue), raised on a rainy Monday
            out.append({"asset_group": "TOA", "initiated_date": "2026/09/07 10:00:00+00", "closed_date": "2026/09/08 10:00:00+00"})
        for i in range(25):      # 25 slower closures: 3 working days, raised on a dry Tuesday
            out.append({"asset_group": "TOA", "initiated_date": "2026/09/01 10:00:00+00", "closed_date": "2026/09/04 10:00:00+00"})
        out.append({"asset_group": "TOA", "initiated_date": "2023/01/03 10:00:00+00", "closed_date": "2024/05/03 10:00:00+00"})
        return out

    def test_baseline_excludes_bulk_closures_and_compares_weather(self):
        h = scoring.history_baseline(self.rows(), {date(2026, 9, 7): "heavy rain"})
        g = h["by_asset_group"][0]
        self.assertEqual((g["closed_in_sample"], g["closed_within_cap"]), (56, 55))
        self.assertEqual(g["median_working_days_within_cap"], 1.0)
        w = h["weather_effect"]
        self.assertEqual((w["orders_raised_on_weather_days"], w["orders_raised_on_other_days"]), (30, 25))
        self.assertEqual((w["median_days_weather"], w["median_days_other"]), (1.0, 3.0))
        self.assertTrue(w["enough_data"])

    def test_small_samples_are_not_enough(self):
        h = scoring.history_baseline(self.rows()[:10], {date(2026, 9, 7): "heavy rain"})
        self.assertFalse(h["weather_effect"]["enough_data"])
