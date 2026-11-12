"""recur.py 的纯函数单元测试：不启动服务、不接触数据库。"""
import copy
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import recur


class ValidateRecurrenceTests(unittest.TestCase):
    def assert_invalid(self, rec, fragment):
        with self.assertRaises(ValueError) as ctx:
            recur.validate_recurrence(rec)
        self.assertIn(fragment, str(ctx.exception))
        # 所有错误消息必须是中文人话且非空。
        self.assertTrue(str(ctx.exception).strip())

    def test_valid_minimal_and_defaults(self):
        # 合法规则不抛错；interval 缺省为 1 由 next_due 消费。
        self.assertIsNone(recur.validate_recurrence({"freq": "daily"}))
        self.assertIsNone(recur.validate_recurrence({
            "freq": "weekly", "interval": 2, "byday": ["MO", "WE", "FR"],
            "until": "2026-12-31",
        }))
        self.assertIsNone(recur.validate_recurrence({
            "freq": "monthly", "count": 5, "completed": 4,
        }))

    def test_freq_must_be_known(self):
        self.assert_invalid({"freq": "hourly"}, "freq")
        self.assert_invalid({}, "freq")
        self.assert_invalid("daily", "对象")

    def test_interval_must_be_positive_int(self):
        self.assert_invalid({"freq": "daily", "interval": 0}, "interval")
        self.assert_invalid({"freq": "daily", "interval": -2}, "interval")
        self.assert_invalid({"freq": "daily", "interval": 1.5}, "interval")
        self.assert_invalid({"freq": "daily", "interval": "1"}, "interval")
        self.assert_invalid({"freq": "daily", "interval": True}, "interval")

    def test_byday_only_weekly_and_weekday_subset(self):
        self.assert_invalid({"freq": "daily", "byday": ["MO"]}, "weekly")
        self.assert_invalid({"freq": "monthly", "byday": ["MO"]}, "weekly")
        self.assert_invalid({"freq": "weekly", "byday": []}, "byday")
        self.assert_invalid({"freq": "weekly", "byday": ["XX"]}, "byday")
        self.assert_invalid({"freq": "weekly", "byday": ["mo"]}, "byday")
        self.assert_invalid({"freq": "weekly", "byday": ["MO", "MO"]}, "重复")
        self.assert_invalid({"freq": "weekly", "byday": ["MO"] * 8}, "byday")
        self.assert_invalid({"freq": "weekly", "byday": "MO"}, "byday")

    def test_until_and_count_rules(self):
        self.assert_invalid({"freq": "daily", "until": "2026-13-01"}, "until")
        self.assert_invalid({"freq": "daily", "until": "31/12/2026"}, "until")
        self.assert_invalid({"freq": "daily", "count": 0}, "count")
        self.assert_invalid({"freq": "daily", "count": 1.0}, "count")
        self.assert_invalid({"freq": "daily", "count": False}, "count")
        self.assert_invalid(
            {"freq": "daily", "until": "2026-12-31", "count": 5}, "互斥"
        )

    def test_completed_must_be_non_negative_int(self):
        self.assert_invalid({"freq": "daily", "completed": -1}, "completed")
        self.assert_invalid({"freq": "daily", "completed": 1.5}, "completed")
        self.assert_invalid({"freq": "daily", "completed": False}, "completed")
        # completed=0 合法。
        self.assertIsNone(recur.validate_recurrence({"freq": "daily", "completed": 0}))


class NextDueTests(unittest.TestCase):
    def test_daily_interval(self):
        self.assertEqual(recur.next_due({"freq": "daily"}, "2026-11-07"), "2026-11-08")
        self.assertEqual(
            recur.next_due({"freq": "daily", "interval": 3}, "2026-11-07"), "2026-11-10"
        )

    def test_weekly_without_byday_adds_interval_weeks(self):
        # 2026-11-07 是周六。
        self.assertEqual(recur.next_due({"freq": "weekly"}, "2026-11-07"), "2026-11-14")
        self.assertEqual(
            recur.next_due({"freq": "weekly", "interval": 2}, "2026-11-07"), "2026-11-21"
        )

    def test_weekly_byday_jumps_to_next_match(self):
        # 周六的下一个周一是 11-09。
        self.assertEqual(
            recur.next_due({"freq": "weekly", "byday": ["MO"]}, "2026-11-07"), "2026-11-09"
        )
        # 多个候选取最近：周一之后是周三 11-11。
        self.assertEqual(
            recur.next_due({"freq": "weekly", "byday": ["MO", "WE"]}, "2026-11-09"),
            "2026-11-11",
        )
        # 当天命中也要严格跳到下一次：周一的下一个周一是 11-16。
        self.assertEqual(
            recur.next_due({"freq": "weekly", "byday": ["MO"]}, "2026-11-09"),
            "2026-11-16",
        )
        # 周末集合跨周回绕：周五 11-13 之后是周六 11-14。
        self.assertEqual(
            recur.next_due({"freq": "weekly", "byday": ["SA", "SU"]}, "2026-11-13"),
            "2026-11-14",
        )

    def test_monthly_clamps_to_month_end(self):
        self.assertEqual(
            recur.next_due({"freq": "monthly"}, "2026-01-31"), "2026-02-28"
        )
        # 收敛后按新日期继续：2/28 -> 3/28。
        self.assertEqual(
            recur.next_due({"freq": "monthly"}, "2026-02-28"), "2026-03-28"
        )
        self.assertEqual(
            recur.next_due({"freq": "monthly", "interval": 2}, "2026-01-31"),
            "2026-03-31",
        )
        # 普通月份同日推进。
        self.assertEqual(
            recur.next_due({"freq": "monthly"}, "2026-11-25"), "2026-12-25"
        )

    def test_yearly_leap_day_clamps_to_feb_28(self):
        self.assertEqual(
            recur.next_due({"freq": "yearly"}, "2024-02-29"), "2025-02-28"
        )
        # 闰年间隔 4 年仍可落在 2/29。
        self.assertEqual(
            recur.next_due({"freq": "yearly", "interval": 4}, "2024-02-29"),
            "2028-02-29",
        )
        self.assertEqual(
            recur.next_due({"freq": "yearly"}, "2026-03-08"), "2027-03-08"
        )

    def test_until_bounds(self):
        # 候选日等于 until 仍允许，晚于 until 才耗尽。
        self.assertEqual(
            recur.next_due(
                {"freq": "daily", "interval": 5, "until": "2026-11-15"}, "2026-11-10"
            ),
            "2026-11-15",
        )
        self.assertIsNone(
            recur.next_due(
                {"freq": "daily", "interval": 5, "until": "2026-11-14"}, "2026-11-10"
            )
        )

    def test_count_uses_completed_occurrences(self):
        # count 含首次到期：completed=0/1 仍有下一次，completed=2 时耗尽。
        rec = {"freq": "daily", "count": 3}
        self.assertEqual(
            recur.next_due({**rec, "completed": 0}, "2026-11-10"), "2026-11-11"
        )
        self.assertEqual(
            recur.next_due({**rec, "completed": 1}, "2026-11-11"), "2026-11-12"
        )
        self.assertIsNone(
            recur.next_due({**rec, "completed": 2}, "2026-11-12")
        )
        # count=1：首次完成即耗尽，没有下一次。
        self.assertIsNone(
            recur.next_due({"freq": "daily", "count": 1}, "2026-11-10")
        )
        # 缺省 completed 按 0 处理。
        self.assertEqual(
            recur.next_due({"freq": "daily", "count": 2}, "2026-11-10"),
            "2026-11-11",
        )

    def test_invalid_from_date_falls_back_to_today(self):
        expected = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(recur.next_due({"freq": "daily"}, "not-a-date"), expected)
        self.assertEqual(recur.next_due({"freq": "daily"}, None), expected)

    def test_recurrence_dict_is_not_mutated(self):
        rec = {"freq": "daily", "count": 3, "completed": 1, "until": None}
        snapshot = copy.deepcopy(rec)
        recur.next_due(rec, "2026-11-10")
        self.assertEqual(rec, snapshot)


class DescribeRecurrenceTests(unittest.TestCase):
    def test_freq_and_interval_wording(self):
        self.assertEqual(recur.describe_recurrence({"freq": "daily"}), "每天")
        self.assertEqual(recur.describe_recurrence({"freq": "daily", "interval": 2}), "每 2 天")
        self.assertEqual(recur.describe_recurrence({"freq": "weekly"}), "每周")
        self.assertEqual(recur.describe_recurrence({"freq": "weekly", "interval": 2}), "每 2 周")
        self.assertEqual(recur.describe_recurrence({"freq": "monthly"}), "每月")
        self.assertEqual(recur.describe_recurrence({"freq": "monthly", "interval": 3}), "每 3 个月")
        self.assertEqual(recur.describe_recurrence({"freq": "yearly"}), "每年")
        self.assertEqual(recur.describe_recurrence({"freq": "yearly", "interval": 2}), "每 2 年")

    def test_weekly_byday_wording_is_ordered(self):
        self.assertEqual(
            recur.describe_recurrence({"freq": "weekly", "interval": 2, "byday": ["MO", "WE"]}),
            "每 2 周的周一、周三",
        )
        # 传入顺序混乱时按周一到周日稳定排序。
        self.assertEqual(
            recur.describe_recurrence({"freq": "weekly", "byday": ["SU", "MO"]}),
            "每周的周一、周日",
        )

    def test_anchor_date_wording(self):
        self.assertEqual(
            recur.describe_recurrence({"freq": "monthly"}, "2026-03-25"), "每月 25 日"
        )
        self.assertEqual(
            recur.describe_recurrence({"freq": "yearly"}, "2026-03-08"), "每年 3 月 8 日"
        )
        # anchor 非法时退回无锚点文案，不抛错。
        self.assertEqual(
            recur.describe_recurrence({"freq": "monthly"}, "bad-date"), "每月"
        )

    def test_until_and_count_suffixes(self):
        self.assertEqual(
            recur.describe_recurrence({"freq": "daily", "until": "2026-12-31"}),
            "每天，直到 2026-12-31",
        )
        self.assertEqual(
            recur.describe_recurrence({"freq": "weekly", "count": 5}),
            "每周，共 5 次",
        )
        self.assertEqual(
            recur.describe_recurrence(
                {"freq": "monthly", "count": 5}, "2026-01-25"
            ),
            "每月 25 日，共 5 次",
        )
        # until 与 count 互斥（validate 已保证），描述函数只负责拼接已有后缀。
        self.assertEqual(
            recur.describe_recurrence(
                {"freq": "yearly", "interval": 2, "until": "2030-12-31"}, "2026-03-08"
            ),
            "每 2 年的 3 月 8 日，直到 2030-12-31",
        )


if __name__ == "__main__":
    unittest.main()
