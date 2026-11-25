"""题目显式给出的参考日：回顾最近记录时作为时间上界。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_RETROSPECTIVE = re.compile(
    r"\bmost\s+recent(?:ly)?\b|\blatest\b|\blast\s+(?:time|trip|visit|stay|occasion)\b"
    r"|最近(?:一次|的)?|上次|最后一次",
    re.IGNORECASE,
)
_REFERENCE = re.compile(
    r"(?:\btoday\s+is\b|\bas\s+of\b|今天(?:是|为))\s*"
    r"((?:19|20)\d{2}(?:[-/.年])\d{1,2}(?:[-/.月])\d{1,2}日?"
    r"|[A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*(?:19|20)\d{2})",
    re.IGNORECASE,
)


def retrospective_reference_day(question: str) -> datetime | None:
    """只取明确日期与回顾语义；多参考日或无法解析时留给原规划。"""
    if not _RETROSPECTIVE.search(question):
        return None
    days = set()
    for match in _REFERENCE.finditer(question):
        raw = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", match.group(1), flags=re.IGNORECASE)
        raw = re.sub(r"[年月/.]", "-", raw).rstrip("日") if raw[0].isdigit() else raw.replace(".", "")
        raw = raw.replace(",", "")
        for fmt in ("%Y-%m-%d", "%B %d %Y", "%b %d %Y"):
            try:
                days.add(datetime.strptime(raw, fmt))
                break
            except ValueError:
                continue
    return days.pop() if len(days) == 1 else None


def cap_at_reference_day(
    bounds: tuple[datetime, datetime] | None, day: datetime
) -> tuple[datetime, datetime]:
    """保留已有起点与更窄的终点；参考日含当天，按已有查询的日历时区。"""
    ceiling = day + timedelta(days=1)
    if bounds is None:
        return datetime.min, ceiling
    start, end = bounds
    return start, min(end, ceiling.replace(tzinfo=end.tzinfo))
