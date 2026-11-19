"""Winning vs weakest profiles: ranking, disjoint picks, the 150-matured floor and the group aggregates."""

from datetime import datetime

from app.stats import leaders

T = datetime(2026, 10, 1)


def _p(name, matured, interviews, region="EU", rows=None):
    return {"accountId": name.lower(), "name": name, "owner": "Ana", "region": region, "matured": matured,
            "interviews": interviews, "per100": round(interviews / matured * 100, 1) if matured else None,
            "rows": rows or []}


def test_best_and_weakest_by_per100():
    team = [_p(n, 200, iv) for n, iv in (("A", 1), ("B", 2), ("C", 3), ("D", 4), ("E", 5), ("F", 6), ("G", 7))]
    team.append(_p("H", 300, 21))  # 7.0 per 100: best
    team.append(_p("G2", 400, 14))  # 3.5 ties G; more matured first
    out = leaders.pick(team)
    assert [p["name"] for p in out["best"]] == ["H", "G2", "G"]
    assert [p["name"] for p in out["weakest"]] == ["A", "B", "C"]
    assert set(out["best"][0]) == {"accountId", "name", "owner", "region", "matured", "interviews", "per100"}


def test_leaders_disjoint_when_few():
    out = leaders.pick([_p("A", 200, 1), _p("B", 200, 2), _p("C", 200, 3), _p("D", 200, 4)])
    assert [p["name"] for p in out["best"]] == ["D", "C", "B"]
    assert [p["name"] for p in out["weakest"]] == ["A"]
    assert [p["name"] for p in leaders.pick([_p("A", 200, 1)])["weakest"]] == []


def test_min_150_matured():
    out = leaders.pick([_p("A", 149, 9), _p("B", 150, 1)])
    assert [p["name"] for p in out["best"]] == ["B"] and out["weakest"] == []


def _r(by="self", score=None, area=None, board=None, age=None, days=None):
    return {"by": by, "score": score, "area": area, "board": board, "profileAgeDays": age,
            "interviewId": "iv" if days is not None else None, "daysToInterview": days, "appliedAt": T}


def test_group_aggregates():
    rows_a = ([_r(score=80, area="Backend", board="ashbyhq.com", age=10, days=4) for _ in range(6)]
              + [_r("bidder", area="Frontend", board="lever.co", age=30) for _ in range(4)])
    rows_b = [_r("bidder", score=60, area="Backend", board="ashbyhq.com", age=50, days=8) for _ in range(5)] + [
        _r("bidder", board="myworkdayjobs.com") for _ in range(5)]
    out = leaders.pick([_p("A", 200, 6, "EU", rows_a), _p("B", 200, 5, "US", rows_b)])
    g = out["groups"]["best"]
    assert g["matured"] == 400 and g["interviews"] == 11
    assert g["regions"] == {"EU": 1, "US": 1}
    assert g["avgScore"] == 70.9 and g["scored"] == 11
    assert g["areas"] == [{"label": "Backend", "share": 73}, {"label": "Frontend", "share": 27}]
    assert g["areaKnown"] == 75
    assert g["medianProfileAge"] == 30
    assert g["sites"] == [{"label": "Ashby", "share": 55}, {"label": "Workday", "share": 25},
                          {"label": "Lever", "share": 20}]
    assert g["medianDays"] == 4 and g["interviewCount"] == 11 and g["daysTooFew"] is False
    assert g["selfShare"] == 30
    assert out["groups"]["weakest"]["matured"] == 0 and out["groups"]["weakest"]["daysTooFew"] is True
