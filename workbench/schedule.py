"""中文定时规则：纯函数模块，无 IO、无数据库、无第三方依赖。

定时规则与子任务、重复设置一样收纳在 items.meta 自由 JSON 中（字段名
``remindSchedule``）。本模块只负责规则校验、「今天是否该响」判断与中文描述，
不读取也不修改任何外部状态，SQLite 与 PostgreSQL 部署共用同一套语义。

规则字段（用户在界面上只看到中文选项，不接触任何 cron 表达式）：
- kind:      daily（每天）/ weekdays（工作日）/ weekly（每周指定几天）/ monthly（每月几号）
- time:      "HH:MM"，24 小时制，所有规则都必须给出
- weekdays:  仅 weekly，MO..SU 的去重非空子集
- monthDay:  仅 monthly，1..31 的整数；当月没有这一天时收敛到该月最后一天
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime

KINDS = frozenset(("daily", "weekdays", "weekly", "monthly"))
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_SET = frozenset(WEEKDAYS)
# Python date.weekday() 以周一为 0，与 WEEKDAYS 的顺序一致。
_WEEKDAY_INDEX = {name: index for index, name in enumerate(WEEKDAYS)}
_WEEKDAY_CN = {
    "MO": "周一", "TU": "周二", "WE": "周三", "TH": "周四",
    "FR": "周五", "SA": "周六", "SU": "周日",
}
_TIME_RE = re.compile(r"^([0-2]\d):([0-5]\d)$")
_DATE_FORMAT = "%Y-%m-%d"


def _is_int(value):
    # bool 是 int 的子类，规则字段一律不接受布尔值。
    return isinstance(value, int) and not isinstance(value, bool)


def validate_remind_schedule(rule):
    """校验定时规则 dict；合法返回 None，非法抛 ValueError（中文消息）。"""
    if not isinstance(rule, dict):
        raise ValueError("定时规则必须是对象")
    kind = rule.get("kind")
    if kind not in KINDS:
        raise ValueError("定时规则类型必须是每天、工作日、每周或每月之一")
    time_value = rule.get("time")
    if not isinstance(time_value, str):
        raise ValueError("定时提醒时间必须是 HH:MM 格式的字符串")
    match = _TIME_RE.fullmatch(time_value.strip())
    if not match or int(match[1]) > 23:
        raise ValueError("定时提醒时间格式应为 HH:MM（24 小时制）")
    if kind == "weekly":
        weekdays = rule.get("weekdays")
        if not isinstance(weekdays, list) or not 1 <= len(weekdays) <= 7:
            raise ValueError("每周规则需要选择 1 至 7 个星期")
        if any(not isinstance(day, str) or day not in WEEKDAY_SET for day in weekdays):
            raise ValueError("星期只能包含 MO、TU、WE、TH、FR、SA、SU")
        if len(set(weekdays)) != len(weekdays):
            raise ValueError("星期选择不能重复")
    elif "weekdays" in rule:
        raise ValueError("只有每周规则才能设置 weekdays")
    if kind == "monthly":
        month_day = rule.get("monthDay")
        if not _is_int(month_day) or not 1 <= month_day <= 31:
            raise ValueError("每月规则需要给出 1 至 31 之间的日期")
    elif "monthDay" in rule:
        raise ValueError("只有每月规则才能设置 monthDay")


def _coerce_date(day):
    """接受 date 对象或 YYYY-MM-DD 字符串；非法返回 None。"""
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    if isinstance(day, str):
        try:
            return datetime.strptime(day, _DATE_FORMAT).date()
        except ValueError:
            return None
    return None


def schedule_due_today(rule, day):
    """判断规则在给定日期当天是否需要提醒；任何非法输入都返回 False（容错）。"""
    if not isinstance(rule, dict):
        return False
    try:
        validate_remind_schedule(rule)
    except ValueError:
        return False
    target = _coerce_date(day)
    if target is None:
        return False
    kind = rule["kind"]
    if kind == "daily":
        return True
    if kind == "weekdays":
        # 周一=0 … 周日=6；工作日即周一至周五。
        return target.weekday() < 5
    if kind == "weekly":
        return target.weekday() in {_WEEKDAY_INDEX[name] for name in rule["weekdays"]}
    # monthly：当月没有这一天（如 2 月没有 31 号）时，收敛到该月最后一天。
    last_day = calendar.monthrange(target.year, target.month)[1]
    effective = min(rule["monthDay"], last_day)
    return target.day == effective


def describe_schedule(rule):
    """把规则翻译成中文人话；非法规则返回空串，调用方可据此不展示。"""
    if not isinstance(rule, dict):
        return ""
    try:
        validate_remind_schedule(rule)
    except ValueError:
        return ""
    time_value = rule["time"].strip()
    kind = rule["kind"]
    if kind == "daily":
        return f"每天 {time_value}"
    if kind == "weekdays":
        return f"工作日（周一至周五）{time_value}"
    if kind == "weekly":
        chosen = set(rule["weekdays"])
        names = "、".join(_WEEKDAY_CN[name] for name in WEEKDAYS if name in chosen)
        return f"每{names} {time_value}"
    return f"每月 {rule['monthDay']} 号 {time_value}"
