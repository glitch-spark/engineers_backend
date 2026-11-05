from datetime import date

from app.bids.performance import previous_range, rank, streaks, week_columns

D = lambda m, d: date(2026, m, d)


def test_week_columns():
    cols = week_columns(D(10, 1), D(10, 31))
    assert len(cols) == 5
    assert cols[0] == {"start": "2026-10-01", "end": "2026-10-04", "partial": True}
    assert cols[1] == {"start": "2026-10-05", "end": "2026-10-11", "partial": False}
    assert cols[-1] == {"start": "2026-10-26", "end": "2026-10-31", "partial": True}


def test_streaks_weekend_does_not_break():
    r = streaks({D(10, 23): 50, D(10, 26): 60, D(10, 27): 55}, D(10, 19), D(10, 30), D(11, 4))
    assert (r["longestStreak"], r["hitDays"], r["weekdays"]) == (3, 3, 10)


def test_streaks_49_breaks():
    c = dict(zip([D(10, d) for d in range(19, 24)], [50, 50, 49, 50, 50]))
    r = streaks(c, D(10, 19), D(10, 23), D(11, 4))
    assert r == {"longestStreak": 2, "currentStreak": 2, "hitDays": 4, "weekdays": 5}


def test_streaks_today():
    c = {D(11, 2): 60, D(11, 3): 60, D(11, 4): 30}
    r = streaks(c, D(11, 2), D(11, 8), D(11, 4))
    assert (r["weekdays"], r["currentStreak"]) == (2, 2)
    c[D(11, 4)] = 50
    r = streaks(c, D(11, 2), D(11, 8), D(11, 4))
    assert (r["weekdays"], r["currentStreak"]) == (3, 3)


def test_streaks_clip_to_active_days():
    assert streaks({}, D(10, 21), D(10, 30), D(11, 4))["weekdays"] == 8


def test_streaks_empty_range():
    assert streaks({}, D(10, 5), D(10, 1), D(11, 4)) == {"longestStreak": 0, "currentStreak": 0, "hitDays": 0, "weekdays": 0}


def test_previous_range():
    assert previous_range("week", D(10, 19), D(10, 25)) == (D(10, 12), D(10, 18))
    assert previous_range("month", D(10, 1), D(10, 31)) == (D(9, 1), D(9, 30))
    assert previous_range("month", D(11, 1), D(11, 4)) == (D(10, 1), D(10, 4))
    assert previous_range("month", D(3, 1), D(3, 31)) == (D(2, 1), D(2, 28))
    assert previous_range("custom", D(9, 1), D(10, 31)) == (D(7, 2), D(8, 31))


def row(id, name, total=0, approved=0, streak=0, rate=0.0, interviews=0, per100=None):
    return {"id": id, "name": name, "total": total, "approved": approved, "longestStreak": streak, "hitRate": rate,
            "interviews": interviews, "per100": per100}


def test_rank():
    rows = [
        row("a", "Zed", 200, 150, 5, 0.5, 4, 2.0), row("b", "Amy", 200, 150, 5, 0.5, 4, 2.0),
        row("c", "Cy", 200, 160, 5, 0.6, 3, 1.5), row("d", "Di", 150, 100, 7, 0.4, 3, 2.0),
        row("e", "Ed", 99, 99, 0, 0.1, 5, 5.0), row("f", "Flo", 0),
    ]
    r = rank(rows)
    assert r["bids"] == ["c", "b", "a"]
    assert r["streak"] == ["d", "c", "a"]  # a, b tie fully: input order
    assert r["interviews"] == ["e", "b", "a"]
    assert r["conversion"] == ["b", "a", "d"]  # e has < 100 bids; Amy before Zed
