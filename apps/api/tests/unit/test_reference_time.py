from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sentrix.domain.assistant.reference_time import cap_at_reference_day, retrospective_reference_day


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Today is Feb 9th, 2023. When did I most recently cook?", datetime(2023, 2, 9)),
        ("As of September 2, 2020, when was my last trip?", datetime(2020, 9, 2)),
        ("今天是2019年8月1日，最近一次去海边是什么时候？", datetime(2019, 8, 1)),
        ("Today is 2024-06-30. What was my latest booking?", datetime(2024, 6, 30)),
        ("When did I most recently cook?", None),
        ("Today is Feb 9th, 2023. What am I planning for next year?", None),
        ("Today is Feb 30, 2023. What was my latest booking?", None),
        ("As of 2020-09-02 or as of 2021-01-01, what was my latest trip?", None),
    ],
)
def test_only_explicit_single_reference_dates_bound_retrospective_questions(question, expected):
    assert retrospective_reference_day(question) == expected


def test_reference_day_caps_existing_ranges_without_widening_them():
    day = datetime(2020, 9, 2)
    assert cap_at_reference_day(None, day) == (datetime.min, datetime(2020, 9, 3))
    earlier = (datetime(2020, 7, 1), datetime(2020, 8, 1))
    assert cap_at_reference_day(earlier, day) == earlier
    zone = ZoneInfo("Asia/Shanghai")
    start = datetime(2020, 9, 1, tzinfo=zone)
    end = datetime(2020, 10, 1, tzinfo=zone)
    assert cap_at_reference_day((start, end), day) == (start, datetime(2020, 9, 3, tzinfo=zone))
