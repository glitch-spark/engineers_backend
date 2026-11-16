import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.bids.windows import BidSchedule, Rules, latest_cutoff, open_window, period_key, schedule_from_settings, window_for
from app.config import Settings

CT = ZoneInfo("America/Chicago")
SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_daily_cutoff_before_and_after_the_time():
    # Fri 2026-10-02 18:30 CDT == 23:30 UTC
    assert latest_cutoff("daily", utc(2026, 10, 2, 23, 29), SCHED) == datetime(2026, 10, 1, 18, 30, tzinfo=CT)
    assert latest_cutoff("daily", utc(2026, 10, 2, 23, 30), SCHED) == datetime(2026, 10, 2, 18, 30, tzinfo=CT)


def test_daily_window_is_one_local_day_in_utc():
    assert window_for("daily", datetime(2026, 10, 2, 18, 30, tzinfo=CT), SCHED) == (
        utc(2026, 10, 1, 23, 30), utc(2026, 10, 2, 23, 30))


def test_daily_window_across_dst_end_is_25_hours():
    start, end = window_for("daily", datetime(2026, 11, 1, 18, 30, tzinfo=CT), SCHED)  # DST ends Sun Nov 1
    assert end - start == timedelta(hours=25)
    assert end == utc(2026, 11, 2, 0, 30)


def test_daily_window_across_dst_start_is_23_hours():
    start, end = window_for("daily", datetime(2026, 3, 8, 18, 30, tzinfo=CT), SCHED)  # DST starts Sun Mar 8
    assert end - start == timedelta(hours=23)


def test_weekly_cutoff_is_most_recent_saturday_4pm():
    # Sat 2026-10-03 16:00 CDT == 21:00 UTC
    assert latest_cutoff("weekly", utc(2026, 10, 3, 20, 59), SCHED) == datetime(2026, 9, 26, 16, 0, tzinfo=CT)
    assert latest_cutoff("weekly", utc(2026, 10, 3, 21, 0), SCHED) == datetime(2026, 10, 3, 16, 0, tzinfo=CT)
    assert latest_cutoff("weekly", utc(2026, 10, 7, 12, 0), SCHED) == datetime(2026, 10, 3, 16, 0, tzinfo=CT)


def test_weekly_window_over_dst_end_is_169_hours():
    start, end = window_for("weekly", datetime(2026, 11, 7, 16, 0, tzinfo=CT), SCHED)
    assert end - start == timedelta(hours=169)


def test_open_window_runs_from_latest_cutoff_to_now():
    now = utc(2026, 10, 3, 3, 0)
    assert open_window("daily", now, SCHED) == (utc(2026, 10, 2, 23, 30), now)


def test_period_key_is_local_date_of_cutoff():
    assert period_key(datetime(2026, 10, 2, 18, 30, tzinfo=CT)) == "2026-10-02"


def test_schedule_from_settings_parses_values():
    s = Settings(_env_file=None, MONGODB_URI="m", AUTH_SECRET="a" * 32, BID_REPORT_TIMEZONE="UTC",
                 BID_DAILY_REPORT_TIME="07:05", BID_WEEKLY_REPORT_DAY="Monday", BID_WEEKLY_REPORT_TIME="09:00")
    assert schedule_from_settings(s) == BidSchedule(tz=ZoneInfo("UTC"), daily_at=time(7, 5), weekly_day=0,
                                                    weekly_at=time(9, 0))


@pytest.mark.parametrize("value,expected", [
    (None, None), ("", None), ("   ", None),
    (" https://s3.us-east-005.backblazeb2.com/ ", "https://s3.us-east-005.backblazeb2.com"),
    # Backblaze's console shows the endpoint without a scheme
    ("s3.us-east-005.backblazeb2.com", "https://s3.us-east-005.backblazeb2.com"),
])
def test_storage_endpoint_url_is_optional_and_trimmed(value, expected):
    s = Settings(_env_file=None, MONGODB_URI="m", AUTH_SECRET="a" * 32, BID_STORAGE_ENDPOINT_URL=value)
    assert s.BID_STORAGE_ENDPOINT_URL == expected


@pytest.mark.parametrize("field,value", [
    ("BID_DAILY_REPORT_TIME", "25:00"), ("BID_DAILY_REPORT_TIME", "6:30pm"), ("BID_WEEKLY_REPORT_TIME", "16"),
    ("BID_WEEKLY_REPORT_DAY", "funday"), ("BID_REPORT_TIMEZONE", "Mars/Base"),
])
def test_invalid_schedule_settings_fail_validation(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, MONGODB_URI="m", AUTH_SECRET="a" * 32, **{field: value})


_SWITCH = dict(BID_SCHEDULE_SWITCH_AT="2026-11-22T20:00", BID_NEXT_DAILY_REPORT_TIME="18:00",
               BID_NEXT_WEEKLY_REPORT_DAY="sunday", BID_NEXT_WEEKLY_REPORT_TIME="20:00")


def _settings(**over):
    return Settings(_env_file=None, MONGODB_URI="m", AUTH_SECRET="a" * 32, **over)


def test_switch_settings_build_a_two_rule_schedule():
    sched = schedule_from_settings(_settings(**_SWITCH))
    assert sched.switch_at == datetime(2026, 11, 22, 20, 0, tzinfo=CT)
    assert sched.next_rules == Rules(time(18, 0), 6, time(20, 0))
    assert (sched.tz, sched.daily_at, sched.weekly_day, sched.weekly_at) == (CT, time(18, 30), 5, time(16, 0))


def test_switch_off_by_default():
    sched = schedule_from_settings(_settings())
    assert sched.switch_at is None and sched.next_rules is None


def test_blank_switch_values_mean_off():
    sched = schedule_from_settings(_settings(BID_SCHEDULE_SWITCH_AT=" ", BID_NEXT_DAILY_REPORT_TIME="",
                                             BID_NEXT_WEEKLY_REPORT_DAY="", BID_NEXT_WEEKLY_REPORT_TIME=""))
    assert sched.switch_at is None and sched.next_rules is None


@pytest.mark.parametrize("missing", ["BID_NEXT_DAILY_REPORT_TIME", "BID_NEXT_WEEKLY_REPORT_DAY",
                                     "BID_NEXT_WEEKLY_REPORT_TIME"])
def test_switch_needs_all_next_values(missing):
    values = {k: v for k, v in _SWITCH.items() if k != missing}
    with pytest.raises(ValidationError):
        _settings(**values)


@pytest.mark.parametrize("override", [
    {"BID_SCHEDULE_SWITCH_AT": "2026-11-22T19:00"},  # right day, wrong time
    {"BID_SCHEDULE_SWITCH_AT": "2026-11-21T20:00"},  # a Saturday
    {"BID_NEXT_WEEKLY_REPORT_DAY": "saturday"},
    {"BID_NEXT_WEEKLY_REPORT_TIME": "19:00"},
])
def test_switch_must_sit_on_the_next_weekly_cutoff(override):
    with pytest.raises(ValidationError):
        _settings(**{**_SWITCH, **override})


@pytest.mark.parametrize("override", [
    {"BID_SCHEDULE_SWITCH_AT": "2026-11-22 20:00"}, {"BID_SCHEDULE_SWITCH_AT": "2026-11-22"},
    {"BID_SCHEDULE_SWITCH_AT": "2026-02-30T20:00"}, {"BID_NEXT_DAILY_REPORT_TIME": "25:00"},
    {"BID_NEXT_WEEKLY_REPORT_DAY": "funday"},
])
def test_invalid_switch_values_fail_validation(override):
    with pytest.raises(ValidationError):
        _settings(**{**_SWITCH, **override})


def _bid_env_values(text: str) -> dict:
    import io

    from dotenv import dotenv_values

    values = dotenv_values(stream=io.StringIO(text))
    return {k: v for k, v in values.items() if k.startswith(("BID_", "SLACK_BIDS_"))}


def test_env_examples_have_no_inline_comments_in_bid_values():
    # python-dotenv reads `KEY=   # note` as the value "# note", so comments go on their own lines.
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    example = _bid_env_values((root / ".env.example").read_text(encoding="utf-8"))
    doc = (root / "docs" / "bids.md").read_text(encoding="utf-8")
    doc_env = _bid_env_values(doc.split("```env", 1)[1].split("```", 1)[0])
    assert len(example) == 12 and set(doc_env) == set(example)
    for values in (example, doc_env):
        assert all("#" not in (v or "") for v in values.values()), values
