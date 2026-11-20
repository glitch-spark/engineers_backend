"""Statistics → Interviews report: round outcomes, group tables, reasons, follow-ups and the summary (pure, no
DB). Definitions and the response shape: docs/superpowers/specs/2026-11-20-interview-statistics-design.md."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from app.caller_stats import ALIASES, name_key, typed_caller
from app.interview_normalize import _utc_naive, normalize_stage
from app.interview_reasons import CANCEL_REASONS, REJECT_REASONS
from app.interview_rounds import merged_rounds
from app.slack_bot import _ROUND_LABELS  # the stages in pipeline order, labelled as on the Interviews board
from app.stats import frame
from app.stats.conversion import _rate
from app.stats.job_fit import _AREAS, _SCORES, NO_AREA

FEW_DECIDED = 20  # a pass rate over fewer decided rounds is too thin to read
NO_ANSWER_AFTER = timedelta(days=14)
RESULT_WAIT = timedelta(days=7)
RESCHEDULE_LOG_START = date(2026, 11, 20)  # reschedules are logged from this day
HELD = ("passed", "rejected", "no_answer", "waiting")
DECIDED = HELD[:3]
UNFILLED = ("none", "Reason not filled in yet")
DELETED_PROFILE = {"name": "Deleted profile", "region": None}
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_REGIONS = [("Latam", "Latam"), ("EU", "EU"), ("US", "US"), ("unknown", "Unknown")]
_BANDS = {k for k, _ in _SCORES}


def outcome(status: str, reason: Optional[str], latest: bool, end: datetime, now: datetime) -> str:
    """Exactly one of canceled / scheduled / passed / rejected / no_answer / waiting. `latest`: no later round in the
    interview (whatever its status)."""
    if status in ("canceled", "scheduled"):
        return status
    if status == "passed" or not latest:
        return "passed"
    if status == "rejected":
        return "no_answer" if reason == "no_answer" else "rejected"
    return "no_answer" if end <= now - NO_ANSWER_AFTER else "waiting"


def caller_index(directory: list) -> tuple[dict, dict]:
    """The caller directory (caller_directory.list_directory, archived callers too) by id and by name key. A platform
    user and a list caller of one name are one person: the list caller."""
    return {e["_id"]: e for e in directory}, {name_key(e["name"]): e for e in directory}


def caller_person(caller, index: tuple[dict, dict]) -> Optional[tuple]:
    """(key, label) of the person a round's caller is, or None when no caller is assigned (caller off, or on with no
    id and a blank / TBD name). One person whatever the way the round stores them: an id of the caller list, an id of
    a platform user, or a typed name (caller_stats.typed_caller: aliases too) all resolve to the directory entry of
    that name, labelled with its current name. A caller no longer in the directory is keyed by the typed name."""
    if caller is None or not caller.enabled:
        return None
    by_id, by_name = index
    name = " ".join((caller.callerName or "").split())
    name = "" if name.upper() == "TBD" else name
    entry = by_id.get(caller.callerId) if caller.callerId else None
    entry = typed_caller(entry["name"] if entry else name, by_name) or entry
    if entry:
        return entry["_id"], entry["name"]
    if name:
        return f"name:{ALIASES.get(name_key(name), name_key(name))}", name
    return (caller.callerId, "Removed caller") if caller.callerId else None


def interview_facts(iv, iv_id, app: Optional[dict], now: datetime, callers: tuple[dict, dict] = ({}, {})) -> dict:
    """What the report needs of one interview (the cached load): its countable, dated rounds in history order and the
    bid-side keys, from its `applications` row (`app`) falling back to resumeLink / fitScore / appliedBy. `callers`:
    `caller_index` of the caller directory."""
    from app.routers.interviews import round_ends_at  # lazy: the routers import app.stats

    entries = {e.id: e for e in iv.stageHistory or []}
    rounds = []
    for r in merged_rounds(iv):
        at, stage = _utc_naive(r.at), normalize_stage(r.stage)
        if stage not in _ROUND_LABELS or at is None:  # AI interview, home assessment, unknown
            continue
        e = entries.get(r.entry_id)
        rounds.append({
            "stage": stage, "at": at, "end": _utc_naive(round_ends_at(at, _utc_naive(e.endsAt if e else iv.endsAt))),
            "status": r.status or ("scheduled" if at > now else "completed"),
            "reason": e.statusReason if e else None,
            "caller": caller_person(r.caller, callers),
            "reschedules": [x.reason for x in e.reschedules] if e else [],
            "followed": bool(e and e.followUps),
            "statusAt": _utc_naive(e.statusAt) if e else None,
        })
    app = app or {}
    link = iv.resumeLink
    band = app.get("scoreBand") or (link.score.band if link and link.score else None) or (iv.fitScore or {}).get("band")
    by = app.get("by")
    if by not in ("self", "bidder"):
        by = iv.appliedBy.kind if iv.appliedBy else "unknown"
    return {"id": iv_id, "accountId": str(iv.accountId), "ownerId": iv.createdBy, "rounds": rounds, "by": by,
            "area": app.get("area") if app.get("area") in _AREAS else None, "scoreBand": band if band in _BANDS else None}


def period_rows(interviews: list[dict], accounts: dict, start: datetime, end: datetime, now: datetime, tz,
                deleted: bool = False) -> list[dict]:
    """One row per round scheduled in [start, end) on a profile of `accounts` (conversion_scope's shape). A round's
    outcome and first-held flag come from the whole interview, whatever the period. `deleted`: `accounts`
    is every profile, so an interview on none of them is on a deleted one and counts, with no region."""
    out = []
    for iv in interviews:
        acct = accounts.get(iv["accountId"]) or (DELETED_PROFILE if deleted else None)
        if not acct or not iv["rounds"]:
            continue
        last = iv["rounds"][-1]
        rs = [{**r, "latest": r is last, "outcome": outcome(r["status"], r["reason"], r is last, r["end"], now)}
              for r in iv["rounds"]]
        for r in rs:
            r["offer"] = r["stage"] == "final" and r["outcome"] == "passed"
        held = [r for r in rs if r["outcome"] in HELD]
        for r in rs:
            if start <= r["at"] < end:
                out.append({**r, "iid": iv["id"], "accountId": iv["accountId"], "ownerId": iv["ownerId"],
                            "profile": acct["name"], "region": acct["region"], "area": iv["area"],
                            "scoreBand": iv["scoreBand"], "by": iv["by"], "firstHeld": bool(held) and r is held[0],
                            "local": r["at"].replace(tzinfo=timezone.utc).astimezone(tz)})
    return out


def own_rows(ivs: list, start: datetime, end: datetime, now: datetime, tz) -> list[dict]:
    """`period_rows` of Interview documents already narrowed to their owner (Home): whatever the profile, with no
    bid-side keys and callers by their typed name."""
    return period_rows([interview_facts(iv, iv.id, None, now) for iv in ivs], {}, start, end, now, tz, deleted=True)


def pass_rate(rows: list[dict]) -> Optional[float]:
    """Unrounded passed ÷ decided × 100 (the team rate the findings compare with); None with nothing decided."""
    c = Counter(r["outcome"] for r in rows)
    decided = sum(c[o] for o in DECIDED)
    return c["passed"] * 100 / decided if decided else None


def counts(rows: list[dict]) -> dict:
    c = Counter(r["outcome"] for r in rows)
    decided = sum(c[o] for o in DECIDED)
    return {"rounds": len(rows), "held": sum(c[o] for o in HELD), "decided": decided, "passed": c["passed"],
            "rejected": c["rejected"], "noAnswer": c["no_answer"], "waiting": c["waiting"], "canceled": c["canceled"],
            "scheduled": c["scheduled"], "rate": _rate(c["passed"], decided), "few": decided < FEW_DECIDED}


def _totals(rows: list[dict]) -> dict:
    return {**counts(rows), "companies": sum(1 for r in rows if r["firstHeld"]),
            "offers": sum(1 for r in rows if r["offer"])}


def _groups(rows: list[dict], keyf: Callable, order: list[tuple], skip_empty: bool = False) -> list[dict]:
    by: dict = defaultdict(list)
    for r in rows:
        by[keyf(r)].append(r)
    return [{"key": k, "label": label, **counts(by[k]), "mark": None} for k, label in order if by[k] or not skip_empty]


def _folded(rows: list[dict], keyf: Callable, labelf: Callable, other: str) -> list[dict]:
    """One group per key, most rounds first; the `few` ones folded into one "Other …" row that lists them."""
    groups = sorted(_groups(rows, keyf, list({keyf(r): labelf(r) for r in rows}.items())),
                    key=lambda g: (-g["rounds"], g["label"] or ""))
    few = [g for g in groups if g["few"]]
    if not few:
        return groups
    keys = {g["key"] for g in few}
    return [g for g in groups if not g["few"]] + [{
        "key": "other", "label": other, **counts([r for r in rows if keyf(r) in keys]), "mark": None,
        "members": few, "memberCount": len(few)}]


def _reasons(keys: list, labels: dict) -> list[dict]:
    """Count and share per reason, most first; unknown / missing keys together as "none", last."""
    c = Counter(k if k in labels else UNFILLED[0] for k in keys)
    order = sorted((k for k in labels if c[k]), key=lambda k: -c[k]) + [UNFILLED[0]] * bool(c[UNFILLED[0]])
    return [{"key": k, "label": labels.get(k, UNFILLED[1]), "count": c[k], "share": round(c[k] / len(keys) * 100, 1)}
            for k in order]


def _part(hour: int) -> str:
    return "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"


def report(rows: list[dict], now: datetime) -> dict:
    """Everything of the response computed from the scope's period rows, but `window`, `summary` and `findings`.
    A group of rounds whose breakdown value is missing has key "unknown" (the app's contract)."""
    logged = [r for r in rows if r["local"].date() >= RESCHEDULE_LOG_START]
    held = [r for r in rows if r["outcome"] in HELD]

    def unanswered_a_week(r: dict) -> bool:
        if r["status"] == "completed":
            return r["latest"] and r["end"] <= now - RESULT_WAIT
        return r["statusAt"] is not None and r["statusAt"] >= r["end"] + RESULT_WAIT

    follow = [("followed_up", "Followed up", [r for r in held if r["followed"]]),
              ("not_followed_up", "Not followed up", [r for r in held if not r["followed"] and unanswered_a_week(r)])]
    return {
        "totals": {"key": "all", "label": "All rounds", **_totals(rows), "mark": None},
        "byStage": _groups(rows, lambda r: r["stage"], list(_ROUND_LABELS.items()), skip_empty=True),
        "byCaller": _folded([r for r in rows if r["caller"]], lambda r: r["caller"][0], lambda r: r["caller"][1],
                            "Other callers"),
        "byProfile": _folded(rows, lambda r: r["accountId"], lambda r: r["profile"], "Other profiles"),
        "byRegion": _groups(rows, lambda r: r["region"] if r["region"] in ("Latam", "EU", "US") else "unknown", _REGIONS,
                            skip_empty=True),
        "byArea": _groups(rows, lambda r: r["area"] or "unknown", [(a or "unknown", a or NO_AREA) for a in _AREAS],
                          skip_empty=True),
        "byScore": _groups(rows, lambda r: r["scoreBand"] or "unknown", [(k or "unknown", label) for k, label in _SCORES]),
        "byAppliedBy": _groups(rows, lambda r: r["by"], [("self", "Self bid"), ("bidder", "Bidder bid")]) + _groups(
            rows, lambda r: r["by"], [("unknown", "Unknown")], skip_empty=True),
        "byWeekday": _groups(rows, lambda r: _DAYS[r["local"].weekday()].lower(), [(d.lower(), d) for d in _DAYS],
                             skip_empty=True),
        "byHour": _groups(rows, lambda r: _part(r["local"].hour),
                          [("morning", "Morning"), ("afternoon", "Afternoon"), ("evening", "Evening")]),
        "reasons": {
            "canceled": _reasons([r["reason"] for r in rows if r["outcome"] == "canceled"], CANCEL_REASONS),
            "rescheduled": _reasons([k for r in rows for k in r["reschedules"]], CANCEL_REASONS),
            # closed as rejected, "No answer from the company" included; not the rounds silent for 14 days
            "rejected": _reasons([r["reason"] for r in rows if r["status"] == "rejected"
                                  and r["outcome"] in ("rejected", "no_answer")], REJECT_REASONS),
            "rescheduleRate": {"rescheduled": sum(1 for r in logged if r["reschedules"]), "rounds": len(logged),
                               "since": RESCHEDULE_LOG_START.isoformat()},
        },
        "followUps": [{"key": k, "label": label, **{f: v for f, v in counts(rs).items()
                                                    if f in ("rounds", "passed", "rejected", "noAnswer", "waiting")}}
                      for k, label, rs in follow],
    }


def summary(team_rows: list[dict], names: dict, members: set, subject_id, team_view: bool, totals: dict) -> dict:
    """Top cards and the Everyone list. `team_rows`: the whole team's period rows (same period and region); `names`:
    uid → display name of every member plus the subject; `totals`: the scope's totals (the cards of the team view).
    Rank / median over members with a round held; pass rate ranked only from FEW_DECIDED decided rounds."""
    by_owner: dict = defaultdict(list)
    for r in team_rows:
        by_owner[r["ownerId"]].append(r)
    people = {u: _totals(by_owner[u]) for u in names}
    pop = [u for u in people if u in members and people[u]["held"] > 0]
    rated = [u for u in pop if not people[u]["few"]]
    vals = {k: [people[u][k] for u in pop] for k in ("companies", "held", "offers")}
    rates = [people[u]["rate"] for u in rated]
    med = {k: frame.distribution(vals[k])["median"] for k in ("companies", "held")}
    team_rate = pass_rate(team_rows)
    team_rate = round(team_rate, 1) if team_rate is not None else None

    def ranks(u) -> dict:
        p = people[u]
        return {**{k: frame.rank_of(p[k], vals[k], True, u in pop) for k in vals},
                "passRate": (None, len(rates)) if p["few"] else frame.rank_of(p["rate"], rates, True, u in rated)}

    if team_view:
        me, r = totals, defaultdict(lambda: (None, None))
    else:
        me, r = people[subject_id], ranks(subject_id)
    cards = {
        "companies": {"value": me["companies"], "rank": r["companies"][0], "of": r["companies"][1],
                      "median": med["companies"],
                      "vsMedian": None if team_view else frame.vs_median(me["companies"], med["companies"])},
        "rounds": {"value": me["rounds"], "held": me["held"], "canceled": me["canceled"], "scheduled": me["scheduled"],
                   "rank": r["held"][0], "of": r["held"][1], "median": med["held"],
                   "vsMedian": None if team_view else frame.vs_median(me["held"], med["held"])},
        "passRate": {"value": me["rate"], "passed": me["passed"], "decided": me["decided"], "waiting": me["waiting"],
                     "rank": r["passRate"][0], "of": r["passRate"][1], "teamRate": team_rate},
        "offers": {"value": me["offers"], "rank": r["offers"][0], "of": r["offers"][1]},
    }
    everyone = [{"userId": str(u), "name": names[u], "companies": p["companies"], "held": p["held"],
                 "decided": p["decided"], "passed": p["passed"], "passRate": p["rate"], "few": p["few"],
                 "offers": p["offers"], "isSubject": u == subject_id,
                 "ranks": {k: v[0] for k, v in ranks(u).items()}}
                for u, p in people.items() if p["held"] > 0]
    everyone.sort(key=lambda e: (-e["companies"], -e["held"], (e["name"] or "").lower()))
    return {"cards": cards, "everyone": everyone}
