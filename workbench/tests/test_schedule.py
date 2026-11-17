"""schedule.py 纯函数测试：中文定时规则的校验、当天命中与中文描述。

无 IO、无数据库、无第三方依赖；用固定日期断言，避免依赖运行当天的星期。
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import schedule


class ValidateRemindScheduleTests(unittest.TestCase):
    def test_valid_kinds(self):
        schedule.validate_remind_schedule({"kind": "daily", "time": "07:30"})
        schedule.validate_remind_schedule({"kind": "weekdays", "time": "00:00"})
        schedule.validate_remind_schedule(
            {"kind": "weekly", "time": "09:00", "weekdays": ["MO", "WE", "FR"]}
        )
        schedule.validate_remind_schedule(
            {"kind": "monthly", "time": "23:59", "monthDay": 31}
        )

    def test_non_dict_rejected(self):
        for bad in (None, "daily", 7, ["daily"], True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    schedule.validate_remind_schedule(bad)

    def test_bad_kind(self):
        for kind in (None, "", "yearly", "DAILY", 1):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    schedule.validate_remind_schedule({"kind": kind, "time": "07:30"})

    def test_bad_time(self):
        for value in (None, "", "7:30", "24:00", "07:60", "ab:30", 730, "07-30"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    schedule.validate_remind_schedule({"kind": "daily", "time": value})

    def test_weekly_requires_weekdays(self):
        with self.assertRaises(ValueError):
            schedule.validate_remind_schedule({"kind": "weekly", "time": "09:00"})
        with self.assertRaises(ValueError):
            schedule.validate_remind_schedule(
                {"kind": "weekly", "time": "09:00", "weekdays": []}
            )
        with self.assertRaises(ValueError):
            schedule.validate_remind_schedule(
                {"kind": "weekly", "time": "09:00", "weekdays": ["XX"]}
            )
        with self.assertRaises(ValueError):
            schedule.validate_remind_schedule(
                {"kind": "weekly", "time": "09:00", "weekdays": ["MO", "MO"]}
            )

    def test_other_kinds_reject_weekdays(self):
        with self.assertRaises(ValueError):
            schedule.validate_remind_schedule(
                {"kind": "daily", "time": "09:00", "weekdays": ["MO"]}
            )

    def test_monthly_requires_month_day(self):
        with self.assertRaises(ValueError):
            schedule.validate_remind_schedule({"kind": "monthly", "time": "09:00"})
        for day in (0, -1, 32, 100, "15", True):
            with self.subTest(day=day):
                with self.assertRaises(ValueError):
                    schedule.validate_remind_schedule(
                        {"kind": "monthly", "time": "09:00", "monthDay": day}
                    )

    def test_other_kinds_reject_month_day(self):
        with self.assertRaises(ValueError):
            schedule.validate_remind_schedule(
                {"kind": "daily", "time": "09:00", "monthDay": 15}
            )


class ScheduleDueTodayTests(unittest.TestCase):
    # 2026-11-16 是周一，2026-11-17 是周二，2026-11-21 是周六，2026-11-22 是周日。
    MONDAY = "2026-11-16"
    TUESDAY = "2026-11-17"
    SATURDAY = "2026-11-21"
    SUNDAY = "2026-11-22"

    def test_daily_hits_every_day(self):
        rule = {"kind": "daily", "time": "07:30"}
        self.assertTrue(schedule.schedule_due_today(rule, self.MONDAY))
        self.assertTrue(schedule.schedule_due_today(rule, self.SATURDAY))
        self.assertTrue(schedule.schedule_due_today(rule, self.SUNDAY))

    def test_weekdays_hits_workdays_only(self):
        rule = {"kind": "weekdays", "time": "09:00"}
        self.assertTrue(schedule.schedule_due_today(rule, self.MONDAY))
        self.assertTrue(schedule.schedule_due_today(rule, self.TUESDAY))
        self.assertFalse(schedule.schedule_due_today(rule, self.SATURDAY))
        self.assertFalse(schedule.schedule_due_today(rule, self.SUNDAY))

    def test_weekly_hits_selected_weekdays(self):
        rule = {"kind": "weekly", "time": "09:00", "weekdays": ["MO", "WE", "FR"]}
        self.assertTrue(schedule.schedule_due_today(rule, self.MONDAY))
        self.assertFalse(schedule.schedule_due_today(rule, self.TUESDAY))
        self.assertFalse(schedule.schedule_due_today(rule, self.SATURDAY))

    def test_monthly_hits_given_day(self):
        rule = {"kind": "monthly", "time": "09:00", "monthDay": 17}
        self.assertTrue(schedule.schedule_due_today(rule, "2026-11-17"))
        self.assertFalse(schedule.schedule_due_today(rule, "2026-11-16"))

    def test_monthly_day_31_clamps_to_month_end(self):
        # 2 月只有 28 天：31 号规则在 2 月月末（28 号）命中。
        rule = {"kind": "monthly", "time": "09:00", "monthDay": 31}
        self.assertTrue(schedule.schedule_due_today(rule, "2026-02-28"))
        self.assertFalse(schedule.schedule_due_today(rule, "2026-02-27"))
        # 11 月有 30 天：31 号规则在 30 号命中。
        self.assertTrue(schedule.schedule_due_today(rule, "2026-11-30"))
        # 有 31 天的月份仍按 31 号当天命中。
        self.assertTrue(schedule.schedule_due_today(rule, "2027-01-31"))

    def test_invalid_rule_never_hits(self):
        self.assertFalse(schedule.schedule_due_today(None, self.MONDAY))
        self.assertFalse(
            schedule.schedule_due_today({"kind": "daily", "time": "bad"}, self.MONDAY)
        )
        self.assertFalse(schedule.schedule_due_today("not-a-dict", self.MONDAY))

    def test_accepts_date_object(self):
        rule = {"kind": "daily", "time": "07:30"}
        self.assertTrue(schedule.schedule_due_today(rule, date(2026, 11, 16)))


class DescribeScheduleTests(unittest.TestCase):
    def test_descriptions_are_chinese(self):
        self.assertEqual(
            schedule.describe_schedule({"kind": "daily", "time": "07:30"}),
            "每天 07:30",
        )
        self.assertEqual(
            schedule.describe_schedule({"kind": "weekdays", "time": "09:00"}),
            "工作日（周一至周五）09:00",
        )
        self.assertEqual(
            schedule.describe_schedule(
                {"kind": "weekly", "time": "09:00", "weekdays": ["MO", "WE", "FR"]}
            ),
            "每周一、周三、周五 09:00",
        )
        self.assertEqual(
            schedule.describe_schedule(
                {"kind": "monthly", "time": "09:00", "monthDay": 15}
            ),
            "每月 15 号 09:00",
        )

    def test_weekly_description_orders_weekdays(self):
        # 乱序输入也按周一到周日输出，文案稳定。
        text = schedule.describe_schedule(
            {"kind": "weekly", "time": "08:00", "weekdays": ["SU", "MO", "SA"]}
        )
        self.assertEqual(text, "每周一、周六、周日 08:00")


if __name__ == "__main__":
    unittest.main()
