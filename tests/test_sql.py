import unittest

from app import schema as S
from app.errors import DataError, RequestError
from app.sqlguard import assert_read_only


class TestSql(unittest.TestCase):
    def test_all_builders_pass_the_read_only_guard(self):
        f = [{"dimension": "ward", "member": "7"}, {"dimension": "priority_level", "member": "0"}]
        for sql in (S.count_sql("fixfirst.work_orders"), S.as_of_sql(), S.total_sql(f), S.breakdown_sql("ward", f),
                    S.workset_sql(f), S.holidays_sql(), S.weather_sql()):
            assert_read_only(sql)

    def test_count_uses_count_1(self):
        self.assertEqual(S.count_sql("fixfirst.work_orders"), "SELECT COUNT(1) AS n FROM fixfirst.work_orders")

    def test_members_are_escaped_not_interpolated(self):
        sql = S.total_sql([{"dimension": "category", "member": "x' OR '1'='1"}])
        self.assertIn("'x'' OR ''1''=''1'", sql)
        assert_read_only(sql)

    def test_unknown_dimension_and_bad_numbers_rejected(self):
        with self.assertRaises(RequestError):
            S.breakdown_sql("password", [])
        with self.assertRaises(RequestError):
            S.filter_sql([{"dimension": "priority_level", "member": "3; DROP TABLE x"}])
        with self.assertRaises(RequestError):
            S.count_sql("fixfirst.work_orders; DROP TABLE x")

    def test_guard_blocks_writes(self):
        for bad in ("DROP VIEW fixfirst.work_orders", "SELECT 1; DELETE FROM x", "CREATE VIEW v AS SELECT 1"):
            with self.assertRaises(DataError):
                assert_read_only(bad)

    def test_none_member_becomes_is_null(self):
        self.assertIn("w.ward IS NULL", S.total_sql([{"dimension": "ward", "member": "(none)"}]))


if __name__ == "__main__":
    unittest.main()
