import csv
import sys
import unittest

from tests.helpers import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import derive_sla  # noqa: E402


def row(group, prio, start, end, canceled="N"):
    return {"record_set": "history_sample", "asset_group": group, "priority": prio, "initiated_date": start,
            "closed_date": end, "is_canceled": canceled}


class TestDerive(unittest.TestCase):
    def test_cell_target_group_fallback_and_assumption(self):
        rows = [row("TOA", "4", "2026/09/01", "2026/09/02") for _ in range(10)]            # 10 x 1 working day
        rows += [row("TOA", "3", "2026/09/01", "2026/09/10") for _ in range(3)]           # 7 working days, too few for the cell
        rows += [row("TOA", "0", "2023/01/02", "2024/05/01")]                              # bulk closure, excluded by the cap
        rows += [row("UFA", "3", "2026/09/01", "2026/09/02", canceled="Y") for _ in range(20)]   # cancelled: ignored
        table = {(r["asset_group"], r["priority_level"]): r for r in derive_sla.derive(rows)}
        self.assertEqual(table[("TOA", 4)]["target_days"], 1)
        self.assertTrue(table[("TOA", 4)]["target_basis"].startswith("empirical: P75 of 10"))
        self.assertEqual(table[("TOA", 3)]["target_days"], 1)             # group-level P75 of 13 closures = 1
        self.assertIn("asset-group level", table[("TOA", 3)]["target_basis"])
        self.assertTrue(table[("UFA", 3)]["target_basis"].startswith("illustrative"))

    def test_higher_priority_never_has_a_longer_target(self):
        for g in derive_sla.GROUPS:
            t = {r["priority_level"]: r["target_days"] for r in derive_sla.derive([]) if r["asset_group"] == g}
            self.assertTrue(t[5] <= t[4] <= t[3] <= t[1], g)

    def test_shipped_policy_file_matches_the_shipped_data(self):
        with open(ROOT / "data" / "work_orders.csv", newline="", encoding="utf-8") as f:
            derived = derive_sla.derive(list(csv.DictReader(f)))
        with open(ROOT / "data" / "sla_policy.csv", newline="", encoding="utf-8") as f:
            shipped = list(csv.DictReader(f))
        self.assertEqual([(r["asset_group"], int(r["priority_level"]), int(r["target_days"])) for r in shipped],
                         [(r["asset_group"], r["priority_level"], r["target_days"]) for r in derived])


if __name__ == "__main__":
    unittest.main()
