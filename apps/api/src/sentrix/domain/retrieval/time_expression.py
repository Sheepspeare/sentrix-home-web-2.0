"""把「2023 年 10 月」这类时间表达解析成区间。纯规则，零 IO。

逐行移植 1.0 ``backend/query_contracts.py:310`` 的 ``parse_time_expression``。

**为什么时间条件非要有它不可。** 时间条件是**硬**的（``conditions.evaluate`` 按
``strictness == HARD`` 决定要不要排除候选），而用户与模型给出的从来不是区间，
是「去年」「2023 年 10 月」这样的说法。少了这一步，「时间硬条件」就只能接受
已经算好的区间——也就是说，**真正的入口没人接**，而接口看上去是通的。

**四档优先级照抄 1.0**，顺序本身是语义：

1. 月范围（``2025年3月-2025年5月`` / ``2025年3月-5月``）
2. 年范围（``2025年-2026年``）
3. 月/日（``2023年10月`` / ``2023-10`` / ``2023年10月5日``）
4. 纯年份（``2023`` / ``2023年``）

**顺序错了会静默出错。** 把「纯年份」提到前面，``2025年3月-2025年5月`` 会被
第 4 档的 ``fullmatch`` 判失败之后落到第 3 档，解析成「2025 年 3 月」一天——
**返回了一个区间、看起来完全正常，只是短了两个月**。

**解析不出来返回 ``None``，不猜。** 调用方拿 ``None`` 就知道这条时间条件
没被理解——那与「解析成今天」是完全不同的两件事。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 范围两端的连接词。1.0 的字面量。
_RANGE_SEPARATORS = "[-~至到]"

_MONTH_RANGE = re.compile(
    rf"((?:19|20)\d{{2}})\s*年?\s*(\d{{1,2}})\s*月?\s*{_RANGE_SEPARATORS}\s*"
    rf"((?:19|20)\d{{2}})?\s*年?\s*(\d{{1,2}})\s*月?"
)
_YEAR_RANGE = re.compile(rf"((?:19|20)\d{{2}})\s*年?\s*{_RANGE_SEPARATORS}\s*((?:19|20)\d{{2}})\s*年?")
_MONTH_DAY = re.compile(r"((?:19|20)\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])?(?:(\d{1,2})\s*日?)?")
_PURE_YEAR = re.compile(r"((?:19|20)\d{2})\s*年?")


def parse_time_expression(value: str | None) -> tuple[datetime, datetime] | None:
    """时间表达 → 左闭右开区间；认不出来返回 ``None``。"""
    cleaned = str(value or "").strip()
    # 显式查询时区写作 2018-04-01[Asia/Shanghai]；省略时按媒体当地日历。
    zone_suffix = re.fullmatch(r"(.*?)\[([A-Za-z_]+(?:/[A-Za-z_+\-]+)+|UTC)\]", cleaned)
    if zone_suffix:
        bounds = parse_time_expression(zone_suffix.group(1))
        try:
            zone = ZoneInfo(zone_suffix.group(2))
        except ZoneInfoNotFoundError:
            return None
        if bounds is None:
            return None
        start = bounds[0].replace(tzinfo=UTC if bounds[0] == datetime.min else zone)
        return start, bounds[1].replace(tzinfo=zone)

    cutoff = re.fullmatch(r"截至\s*((?:19|20)\d{2}-\d{2}-\d{2})", cleaned)
    if cutoff:
        try:
            end = datetime.fromisoformat(cutoff.group(1)) + timedelta(days=1)
        except ValueError:
            return None
        return datetime.min, end

    try:
        return _calendar_bounds(cleaned)
    except (ValueError, OverflowError):
        return None


def _calendar_bounds(cleaned: str) -> tuple[datetime, datetime] | None:
    # 显式区间使用左闭右开 ISO 两端；中文日期范围包含结束日。
    interval = re.fullmatch(r"(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})", cleaned)
    if interval:
        start, end = (datetime.fromisoformat(value) for value in interval.groups())
        return (start, end) if start < end else None
    day_range = re.fullmatch(
        r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})[日号]?\s*(?:至|到|~|—|–|-|\s+to\s+)\s*"
        r"(?:(\d{4})[年./-])?(?:(\d{1,2})[月./-])?(\d{1,2})[日号]?",
        cleaned,
    )
    if day_range:
        y, m, d, ey, em, ed = day_range.groups()
        start = datetime(int(y), int(m), int(d))
        end = datetime(int(ey or y), int(em or m), int(ed)) + timedelta(days=1)
        return (start, end) if start < end else None
    segment = re.fullmatch(r"(\d{4})年(\d{1,2})月(上旬|中旬|下旬)", cleaned)
    if segment:
        y, m = int(segment[1]), int(segment[2])
        segment_day = {"上旬": 1, "中旬": 11, "下旬": 21}[segment[3]]
        end = datetime(y, m, segment_day + 10) if segment_day < 21 else _next_month(y, m)
        return datetime(y, m, segment_day), end
    comparison = re.fullmatch(
        r"(早于|晚于|不早于|不晚于|截至|before |after |on or before |on or after )\s*(\d{4}-\d{2}-\d{2})",
        cleaned,
    )
    if comparison:
        op, raw = comparison.groups()
        day = datetime.fromisoformat(raw)
        if op in {"早于", "before "}:
            return datetime.min, day
        if op in {"不晚于", "截至", "on or before "}:
            return datetime.min, day + timedelta(days=1)
        if op in {"晚于", "after "}:
            day += timedelta(days=1)
        return day, datetime.max
    month_range = _MONTH_RANGE.fullmatch(cleaned)
    if month_range:
        start_year, start_month = int(month_range[1]), int(month_range[2])
        end_year = int(month_range[3]) if month_range[3] else start_year
        start = datetime(start_year, start_month, 1)
        end = _next_month(end_year, int(month_range[4]))
        return (start, end) if start < end else None
    year_range = _YEAR_RANGE.fullmatch(cleaned)
    if year_range:
        start, end = datetime(int(year_range[1]), 1, 1), datetime(int(year_range[2]) + 1, 1, 1)
        return (start, end) if start < end else None
    # fullmatch 保证「2022年3月下旬」等表达不会被截成整月。
    month_day = _MONTH_DAY.fullmatch(cleaned.removesuffix("号"))
    if month_day:
        year, month = int(month_day[1]), int(month_day[2])
        if month_day[3]:
            start = datetime(year, month, int(month_day[3]))
            return start, start + timedelta(days=1)
        return datetime(year, month, 1), _next_month(year, month)
    pure_year = _PURE_YEAR.fullmatch(cleaned)
    if pure_year:
        year = int(pure_year[1])
        return datetime(year, 1, 1), datetime(year + 1, 1, 1)
    return None


def _next_month(year: int, month: int) -> datetime:
    datetime(year, month, 1)
    return datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
