"""RRULE-lite 重复规则：纯函数模块，无 IO、无数据库、无第三方依赖。

重复规则与子任务、多标签一样收纳在 items.meta 自由 JSON 中（字段名
``recurrence``），本模块只负责规则校验、下一次到期日推算与中文描述，
不读取也不修改任何外部状态，SQLite 与 PostgreSQL 部署可共用同一套语义。

支持的规则字段：
- freq:       daily / weekly / monthly / yearly
- interval:   正整数，缺省 1
- byday:      仅 weekly 可用，MO..SU 的去重子集（1..7 项）
- until:      可选，"YYYY-MM-DD"，与 count 互斥
- count:      可选正整数，含首次到期在内的总次数（对齐 RRULE COUNT 语义）
- completed:  可选非负整数，已完成次数，由服务端在滚动时维护
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

FREQUENCIES = frozenset(("daily", "weekly", "monthly", "yearly"))
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WEEKDAY_SET = frozenset(WEEKDAYS)
# Python date.weekday() 以周一为 0，与 WEEKDAYS 的顺序一致。
_WEEKDAY_INDEX = {name: index for index, name in enumerate(WEEKDAYS)}
_WEEKDAY_CN = {
    "MO": "周一", "TU": "周二", "WE": "周三", "TH": "周四",
    "FR": "周五", "SA": "周六", "SU": "周日",
}
_DATE_FORMAT = "%Y-%m-%d"


def _parse_date(value, field):
    """严格解析 YYYY-MM-DD，非法时抛中文 ValueError。"""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 格式的日期字符串")
    try:
        return datetime.strptime(value, _DATE_FORMAT).date()
    except ValueError:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 格式的合法日期") from None


def _is_int(value):
    # bool 是 int 的子类，规则字段一律不接受布尔值。
    return isinstance(value, int) and not isinstance(value, bool)


def validate_recurrence(rec):
    """校验重复规则 dict；合法返回 None，非法抛 ValueError（中文消息）。"""
    if not isinstance(rec, dict):
        raise ValueError("重复规则必须是对象")
    freq = rec.get("freq")
    if freq not in FREQUENCIES:
        raise ValueError("freq 必须是 daily、weekly、monthly 或 yearly 之一")
    interval = rec.get("interval", 1)
    if not _is_int(interval) or interval < 1:
        raise ValueError("interval 必须是不小于 1 的整数")
    if rec.get("byday") is not None:
        if freq != "weekly":
            raise ValueError("byday 仅支持 weekly 频率")
        byday = rec["byday"]
        if not isinstance(byday, list) or not 1 <= len(byday) <= 7:
            raise ValueError("byday 必须是 1 至 7 个星期标识组成的数组")
        if any(not isinstance(day, str) or day not in WEEKDAY_SET for day in byday):
            raise ValueError("byday 只能包含 MO、TU、WE、TH、FR、SA、SU")
        if len(set(byday)) != len(byday):
            raise ValueError("byday 不能包含重复的星期标识")
    has_until = rec.get("until") is not None
    has_count = rec.get("count") is not None
    if has_until:
        _parse_date(rec["until"], "until")
    if has_count:
        if not _is_int(rec["count"]) or rec["count"] < 1:
            raise ValueError("count 必须是不小于 1 的整数")
    if has_until and has_count:
        raise ValueError("until 与 count 互斥，不能同时设置")
    if rec.get("completed") is not None:
        if not _is_int(rec["completed"]) or rec["completed"] < 0:
            raise ValueError("completed 必须是不小于 0 的整数")


def _add_months(day, months):
    """加若干个日历月；目标月没有同一天时收敛到该月最后一天。"""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _add_years(day, years):
    """加若干个日历年；2 月 29 日在平年收敛到 2 月 28 日。"""
    year = day.year + years
    try:
        return day.replace(year=year)
    except ValueError:
        return date(year, 2, 28)


def next_due(rec, from_date_str):
    """以 from_date 为当前到期日，推算下一次到期日；耗尽时返回 None。

    from_date 非法（含类型错误）时退回今天，保证服务端不会因脏数据报错。
    返回值为 "YYYY-MM-DD" 字符串，全程纯 datetime 计算、无副作用。
    """
    try:
        start = datetime.strptime(from_date_str, _DATE_FORMAT).date()
    except (TypeError, ValueError):
        start = date.today()
    freq = rec["freq"]
    interval = rec.get("interval", 1)
    if freq == "daily":
        candidate = start + timedelta(days=interval)
    elif freq == "weekly":
        byday = rec.get("byday")
        if byday:
            # 严格找“下一个”命中星期：从次日起逐日扫描，非空子集保证 7 天内命中。
            targets = {_WEEKDAY_INDEX[day] for day in byday}
            candidate = start + timedelta(days=1)
            while candidate.weekday() not in targets:
                candidate += timedelta(days=1)
        else:
            candidate = start + timedelta(weeks=interval)
    elif freq == "monthly":
        candidate = _add_months(start, interval)
    else:  # yearly
        candidate = _add_years(start, interval)
    if rec.get("until") is not None and candidate > _parse_date(rec["until"], "until"):
        return None
    count = rec.get("count")
    if count is not None:
        completed = rec.get("completed") or 0
        # RRULE COUNT 含首次到期：当前到期日是第 completed+1 次，下一次将是
        # 第 completed+2 次；它超过 count 时不再安排，任务在本次完成后耗尽。
        if completed + 1 >= count:
            return None
    return candidate.isoformat()


def describe_recurrence(rec, anchor=None):
    """把规则翻译成中文人话；anchor 可选（date 或 YYYY-MM-DD 字符串）。

    月/年规则的“几日/几月几日”依赖具体到期日，规则 dict 本身不含锚点，
    因此无 anchor 时只输出频率部分，有 anchor 时补充日期。
    """
    freq = rec["freq"]
    interval = rec.get("interval", 1)
    anchor_date = None
    if isinstance(anchor, date):
        # datetime 是 date 的子类，同样适用。
        anchor_date = anchor
    elif isinstance(anchor, str):
        try:
            anchor_date = datetime.strptime(anchor, _DATE_FORMAT).date()
        except ValueError:
            anchor_date = None
    if freq == "daily":
        text = "每天" if interval == 1 else f"每 {interval} 天"
    elif freq == "weekly":
        byday = rec.get("byday")
        prefix = "每周" if interval == 1 else f"每 {interval} 周"
        if byday:
            names = "、".join(_WEEKDAY_CN[day] for day in WEEKDAYS if day in set(byday))
            text = f"{prefix}的{names}"
        else:
            text = prefix
    elif freq == "monthly":
        if anchor_date is not None:
            text = (f"每月 {anchor_date.day} 日" if interval == 1
                    else f"每 {interval} 个月的 {anchor_date.day} 日")
        else:
            text = "每月" if interval == 1 else f"每 {interval} 个月"
    else:  # yearly
        if anchor_date is not None:
            text = (f"每年 {anchor_date.month} 月 {anchor_date.day} 日" if interval == 1
                    else f"每 {interval} 年的 {anchor_date.month} 月 {anchor_date.day} 日")
        else:
            text = "每年" if interval == 1 else f"每 {interval} 年"
    suffixes = []
    if rec.get("until") is not None:
        suffixes.append(f"直到 {rec['until']}")
    if rec.get("count") is not None:
        suffixes.append(f"共 {rec['count']} 次")
    if suffixes:
        text += "，" + "，".join(suffixes)
    return text
