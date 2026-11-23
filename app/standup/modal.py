"""The standup form ("Write my update" on the 6 PM nudge): a personal Slack modal pre-filled with the person's day
(the same view the app's Daily plan reads), saved through `plan_save.save_day` like the app's Save.

Block ids: `self:<profileId>` (self bids typed for an untracked profile), `item:<itemId>` (a routine or weekly goal:
action `check`, `number` or `log`), `notes`."""

from __future__ import annotations

import json
import logging
import math
from datetime import date
from typing import Optional

from fastapi import HTTPException

from app import self_bids, slack_bot
from app.config import get_settings
from app.coordination import actions
from app.interview_day.blocks import _short
from app.models import UserExtra
from app.models.plan_common import BidsIn, LogEntry, bids_of
from app.plan_bidder import RELEASE_DAY
from app.plan_save import DayIn, ItemResultIn, save_day
from app.routers import daily_plans
from app.routers._plan_access import require_writer

log = logging.getLogger(__name__)

STANDUP_VIEW = "standup_update"
MAX_BLOCKS = 100
MAX_NUMBER = 100000
MAX_NOTES = 2000
MAX_METADATA = 3000  # Slack's cap on private_metadata
MAX_PREFILL = 3000  # Slack's cap on a text input's initial_value
MAX_ANSWER = 500
ANSWER_ERROR = f"Up to {MAX_ANSWER} characters"
MAX_LOG_LINES = 20
MAX_LOG_LINE = 200
LOG_HINT = "One line per profile: Profile name: what changed"
LOG_LINE_ERROR = f"Each line is limited to {MAX_LOG_LINE} characters"
LOG_LINES_ERROR = f"At most {MAX_LOG_LINES} lines"
NOT_LINKED = "Link your Slack ID in Profile first, then click again."
FUTURE = "A day can be followed up from that day on."
NUMBER_ERROR = f"Enter a number from 0 to {MAX_NUMBER}."
WHOLE_ERROR = f"Enter a whole number from 0 to {MAX_NUMBER}."
NOTES_ERROR = f"At most {MAX_NOTES} characters."
FORM_ERROR = "Something went wrong. Try again from the app."
LOADING = "Loading your day…"


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def _fmt(n) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _short(text, 3000)}]}


def _number_input(block_id: str, label: str, value, hint: str = "", *, decimal: bool = True) -> dict:
    element = {"type": "number_input", "action_id": "number", "is_decimal_allowed": decimal, "min_value": "0",
               "max_value": str(MAX_NUMBER)}
    if value is not None:
        element["initial_value"] = _fmt(value)
    block = {"type": "input", "block_id": block_id, "optional": True, "label": _plain(label[:2000]),
             "element": element}
    if hint:
        block["hint"] = _plain(hint)
    return block


def _log_lines(text: Optional[str]) -> list[str]:
    """A log field's lines: trimmed, blank ones dropped."""
    return [ln for raw in (text or "").splitlines() if (ln := raw.strip())]


def _log_prefill(e: dict, names: Optional[dict]) -> tuple[list[str], dict[str, str]]:
    """A log item's prefill lines ("Name: note" for a linked profile) and the {name: profileId} they carry."""
    lines, links = [], {}
    for en in (e["result"] or {}).get("entries") or []:
        name = (names or {}).get(en.get("profileId") or "")
        if name:
            links.setdefault(name, en["profileId"])
        lines.append(f"{name}: {en['note']}" if name else en["note"])
    return lines, links


def _log_entries(text: Optional[str], links: dict) -> list[tuple[Optional[str], str]]:
    """(profileId, note) per non-blank line. A line starting "<prefilled name>: " keeps that profile's id."""
    out = []
    for ln in _log_lines(text):
        for name in sorted(links, key=len, reverse=True):
            note = ln[len(name) + 2:].strip() if ln.startswith(f"{name}: ") else ""
            if note:
                out.append((links[name], note))
                break
        else:
            out.append((None, ln))
    return out


def _links(view: dict, item_id: str) -> dict:
    """The {name: profileId} the form prefilled for a log item (none for a form opened before this existed)."""
    try:
        return json.loads(view.get("private_metadata") or "")["logs"][item_id]
    except (TypeError, ValueError, KeyError, AttributeError):
        return {}


def _stale(view: dict) -> list:
    """Ids of Choice items the form showed with a stored pick that is no longer an option."""
    try:
        return json.loads(view.get("private_metadata") or "")["stale"]
    except (TypeError, ValueError, KeyError, AttributeError):
        return []


def _answer_input(block_id: str, label: str, action: str, value: Optional[str]) -> dict:
    element = {"type": "plain_text_input", "action_id": action, "multiline": True, "max_length": MAX_ANSWER}
    if value:
        element["initial_value"] = value
    return {"type": "input", "block_id": block_id, "optional": True, "label": _plain(label[:2000]), "element": element}


def _item_blocks(e: dict, progress: dict, names: Optional[dict] = None) -> list[dict]:
    """One item's blocks: two for a live Choice (the pick, then its note), one otherwise."""
    result = e["result"] or {}
    if e["kind"] == "choice" and not e["archived"]:
        options = [{"text": _plain(o), "value": o} for o in e["options"]]
        element = {"type": "static_select", "action_id": "choice", "placeholder": _plain("Pick one"), "options": options}
        if (picked := next((o for o in options if o["value"] == result.get("choice")), None)):
            element["initial_option"] = picked  # a stored pick no longer in the list has none: Slack would refuse it
        block = {"type": "input", "block_id": f"item:{e['id']}", "optional": True, "label": _plain(e["text"][:2000]),
                 "element": element}
        if result.get("choice") and "initial_option" not in element:
            block["hint"] = _plain(_short(f"Current: {result['choice']} (no longer an option)", 2000))
        return [block,
                _answer_input(f"note:{e['id']}", f"{e['text']} · note"[:2000], "note", result.get("text"))]
    return [_item_block(e, progress, names)]


def _item_block(e: dict, progress: dict, names: Optional[dict] = None) -> dict:
    result = e["result"] or {}
    unit = "h" if e["kind"] == "hours" else ""
    if e["archived"] and e["kind"] == "log":
        n = len(result.get("entries") or [])
        return {"type": "context", "elements": [_plain(_short(
            f"{e['text']} (removed) · {n} profile{'s' * (n != 1)}", 2000))]}
    if e["archived"] and e["kind"] in ("text", "choice"):
        said = " — ".join(x for x in ((result.get("choice") if e["kind"] == "choice" else None), result.get("text")) if x)
        return {"type": "context", "elements": [_plain(_short(f"{e['text']} (removed) · {said or 'not done'}", 2000))]}
    if e["archived"]:  # removed from the week but holds a result: shown, not editable (resent by _keep_unshown)
        v = result.get("value")
        shown = f"{_fmt(v)}{unit}" if e["kind"] != "check" and v is not None else \
            "done" if result.get("done") else "not done"
        return {"type": "context", "elements": [_plain(_short(f"{e['text']} (removed) · {shown}", 2000))]}
    if e["kind"] == "check":
        option = {"text": _plain("Done today" if e["scope"] == "goal" else "Done"), "value": "done"}
        element = {"type": "checkboxes", "action_id": "check", "options": [option]}
        if result.get("done"):
            element["initial_options"] = [option]
        return {"type": "input", "block_id": f"item:{e['id']}", "optional": True,
                "label": _plain(e["text"]), "element": element}
    if e["kind"] == "text":
        return _answer_input(f"item:{e['id']}", e["text"], "text", result.get("text"))
    if e["kind"] == "log":
        lines, _ = _log_prefill(e, names)
        if len("\n".join(lines)) > MAX_PREFILL:  # Slack refuses a longer initial_value: read-only, entries kept
            return _context(f"{e['text']} · {len(lines)} profiles — edit them in the app")
        element = {"type": "plain_text_input", "action_id": "log", "multiline": True}
        if lines:
            element["initial_value"] = "\n".join(lines)
        return {"type": "input", "block_id": f"item:{e['id']}", "optional": True, "label": _plain(e["text"]),
                "hint": _plain(LOG_HINT), "element": element}
    target = e["target"]
    if e["scope"] == "goal":
        so_far = (progress.get(e["id"]) or {}).get("value", 0)
        hint = f"/ {_fmt(target)}{unit} this week · {_fmt(so_far)}{unit} so far" if target else ""
    else:
        hint = f"/ {_fmt(target)}{unit}" if target else ""
    return _number_input(f"item:{e['id']}", e["text"], result.get("value"), hint)


def _bids_line(d: dict, day: date, names: dict) -> str:
    t, done = d["targets"], d["done"]
    bidder = d["bidsBidderAuto"] if day >= RELEASE_DAY else done["bidsBidder"]
    line = f"*Bids* · Self {done['bidsSelf']} / {t['bidsSelf']} · Bidder {bidder} / {t['bidsBidder']} (Bid Track)"
    tracked = [f"{names.get(pid, 'Profile')} {n}" for pid, n in d["trackedSelfByProfile"].items()
               if pid in d["trackedProfiles"]]
    if tracked:
        line += " · tracked: " + ", ".join(tracked)
    return line


def _interviews_line(d: dict) -> str:
    iv = d["interviews"]
    parts = []
    for who in ("self", "caller"):
        stages = " · ".join(f"{g}({n})" for g, n in iv["stages"].get(who, {}).items())
        parts.append(f"{who.capitalize()} {iv[who]}" + (f" {stages}" if stages else ""))
    return "*Interviews* · " + " · ".join(parts)


async def build_view(user: UserExtra, day: date) -> dict:
    """The form for `user`'s `day`: their draft, or what they saved ("Edit today's update")."""
    d = await daily_plans._view(user.id, day)
    names = {p["id"]: p["name"] for p in d["profiles"]}
    typed = d["done"].get("bidsSelfByProfile") or {}
    body: list[list[dict]] = []  # groups: a Choice item's two blocks are kept or cut together
    logs: dict[str, dict] = {}
    for p in d["profiles"]:
        if p["id"] in d["trackedProfiles"] or (p["archived"] and not typed.get(p["id"])):
            continue  # tracked profiles count themselves; archived ones only while they hold a number
        label = f"{p['name']} ({p['region']}) — self bids" if p.get("region") else f"{p['name']} — self bids"
        body.append([_number_input(f"self:{p['id']}", label, typed.get(p["id"]) or None, decimal=False)])
    for scope, heading in (("routine", "*Routines*"), ("goal", "*Weekly goals* (optional)")):
        items = [e for e in d["items"] if e["scope"] == scope]
        if items:
            body.append([{"type": "section", "text": {"type": "mrkdwn", "text": heading}}])
            for e in items:
                body.append(_item_blocks(e, d["progress"], names))
                if e["kind"] == "log" and not e["archived"] and body[-1][0]["type"] == "input":
                    logs[e["id"]] = _log_prefill(e, names)[1]
    follow_ups = slack_bot.follow_ups_line(d["followUps"], label="*Follow-ups*")
    head = [_bids_line(d, day, names), _interviews_line(d), *([follow_ups] if follow_ups else [])]
    room = MAX_BLOCKS - len(head) - 1  # the context lines and Extra
    total = sum(map(len, body))
    if total > room:
        kept, used = [], 0
        for g in body:
            if used + len(g) > room - 1:
                break
            kept.append(g)
            used += len(g)
        body = [*kept, [_context(f"{total - used} more lines don't fit here — open the app to fill them in.")]]
    body = [b for g in body for b in g]
    shown = {b.get("block_id") for b in body}
    logs = {iid: m for iid, m in logs.items() if f"item:{iid}" in shown}
    # a shown Choice whose stored pick is no longer an option: an untouched submit must keep it (never dropped: tiny)
    stale = [b["block_id"][5:] for b in body if b.get("block_id", "").startswith("item:")
             and b["element"]["type"] == "static_select" and "hint" in b]
    meta = {"day": day.isoformat(), "logs": logs, **({"stale": stale} if stale else {})}
    while logs and len(json.dumps(meta)) > MAX_METADATA:  # an item without a map reads its lines as free text
        del logs[max(logs, key=lambda i: len(json.dumps(logs[i])))]
    notes = {"type": "plain_text_input", "action_id": "notes", "multiline": True, "max_length": MAX_NOTES}
    if d["notes"]:
        notes["initial_value"] = d["notes"]
    return {
        "type": "modal",
        "callback_id": STANDUP_VIEW,
        "private_metadata": json.dumps({"day": day.isoformat(), **({"logs": logs} if logs else {}),
                                        **({"stale": stale} if stale else {})}),
        "title": _plain("Edit today's update" if d["submitted"] else "Daily standup"),
        "submit": _plain("Post update"),
        "close": _plain("Cancel"),
        "blocks": [
            *(_context(line) for line in head),
            *body,
            {"type": "input", "block_id": "notes", "optional": True, "label": _plain("Extra"), "element": notes},
        ],
    }


def _day(view: dict) -> date:
    try:
        return date.fromisoformat(json.loads(view.get("private_metadata") or "")["day"])
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise ValueError("malformed private_metadata") from exc


def _values(view: dict) -> dict:
    values = (view.get("state") or {}).get("values")
    if not isinstance(values, dict):
        raise ValueError("missing state")
    return values


def _parse(raw: Optional[str], *, whole: bool) -> Optional[float]:
    """A typed number, None when blank. ValueError when it is not a number from 0 to MAX_NUMBER (whole if asked)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    n = float(raw)
    if not math.isfinite(n) or n < 0 or n > MAX_NUMBER or (whole and not n.is_integer()):
        raise ValueError(f"bad number {raw!r}")
    return int(n) if whole else n


def read_submission(view: dict) -> DayIn:
    """What a submitted form carries: a result for every item it showed (unticked → not done, blank → no value) and
    a self bids number for every profile it showed (blank → 0). ValueError when the form is malformed."""
    _day(view)
    results: dict[str, ItemResultIn] = {}
    by_profile: dict[str, int] = {}
    notes = ""
    values = _values(view)
    for block_id, acts in values.items():
        if block_id.startswith("item:") and "choice" in acts:  # the pick, plus the note block beside it
            note = (values.get(f"note:{block_id[5:]}") or {}).get("note")
            picked = (acts["choice"].get("selected_option") or {}).get("value")
            keep = not picked and block_id[5:] in _stale(view)  # untouched: choice left unsent, plan_save keeps it
            results[block_id[5:]] = ItemResultIn(
                **({} if keep else {"choice": picked}),
                **({"text": note.get("value") or ""} if note is not None else {}))
        elif block_id.startswith("item:") and "text" in acts:
            results[block_id[5:]] = ItemResultIn(text=acts["text"].get("value") or "")
        elif block_id.startswith("item:") and "check" in acts:
            results[block_id[5:]] = ItemResultIn(done=bool(acts["check"].get("selected_options")))
        elif block_id.startswith("item:") and "log" in acts:  # blank -> [] (sent, clears the stored entries)
            results[block_id[5:]] = ItemResultIn(entries=[
                LogEntry(profileId=pid, note=note)
                for pid, note in _log_entries(acts["log"].get("value"), _links(view, block_id[5:]))])
        elif block_id.startswith("item:"):
            results[block_id[5:]] = ItemResultIn(value=_parse(acts["number"].get("value"), whole=False))
        elif block_id.startswith("self:"):
            by_profile[block_id[5:]] = int(_parse(acts["number"].get("value"), whole=True) or 0)
        elif block_id == "notes":
            notes = (acts["notes"].get("value") or "").strip()
    return DayIn(results=results, done=BidsIn(bidsSelfByProfile=by_profile), notes=notes)


def submission_errors(view: dict) -> dict[str, str]:
    """Field errors to show inline; empty when fine. Never raises (the router calls it inline): a malformed form
    gives {} and the background handler reports it."""
    try:
        try:
            _day(view)
            values = _values(view)
        except ValueError:
            return {}
        errors: dict[str, str] = {}
        for block_id, acts in values.items():
            if block_id.startswith(("item:", "self:")) and "number" in acts:
                whole = block_id.startswith("self:")
                try:
                    _parse(acts["number"].get("value"), whole=whole)
                except ValueError:
                    errors[block_id] = WHOLE_ERROR if whole else NUMBER_ERROR
            elif block_id.startswith("item:") and "log" in acts:
                lines = _log_entries(acts["log"].get("value"), _links(view, block_id[5:]))
                if any(len(note) > MAX_LOG_LINE for _, note in lines):
                    errors[block_id] = LOG_LINE_ERROR
                elif len(lines) > MAX_LOG_LINES:
                    errors[block_id] = LOG_LINES_ERROR
            elif block_id.startswith(("item:", "note:")) and ("text" in acts or "note" in acts):
                if len((acts.get("text") or acts["note"]).get("value") or "") > MAX_ANSWER:
                    errors[block_id] = ANSWER_ERROR
            elif block_id == "notes" and len((acts["notes"].get("value") or "").strip()) > MAX_NOTES:
                errors[block_id] = NOTES_ERROR
        return errors
    except Exception:  # noqa: BLE001
        log.exception("standup form validation failed")
        return {"notes": FORM_ERROR}


async def _keep_unshown(user: UserExtra, day: date, body: DayIn) -> DayIn:
    """What the form did not show (lines past the block limit, tracked profiles' "other applies", focus, typed totals
    of days without profiles) is kept from the saved day, since save_day replaces results and self bids whole."""
    plan = await daily_plans._find(user.id, day)
    if plan is None:
        return body
    switched = await self_bids.switched_profiles(user.id, day)
    typed = {pid: n for pid, n in plan.done.bidsSelfByProfile.items() if pid not in switched}
    by = {**typed, **plan.done.bidsSelfOtherByProfile, **body.done.bidsSelfByProfile}
    # text / choice are left unsent here: plan_save keeps the stored ones (resending a stale choice would be refused)
    kept = {iid: ItemResultIn(done=r.done, value=r.value, entries=r.entries) for iid, r in plan.results.items()}
    return body.model_copy(update={
        "results": {**kept, **body.results},
        "focus": plan.focus,
        "done": BidsIn(bidsSelfByProfile=by) if by else BidsIn(**bids_of(plan.done)),
    })


def _note_view(text: str) -> dict:
    """A form with only a line of text and no submit: loading, or what went wrong."""
    return {"type": "modal", "title": _plain("Daily standup"), "close": _plain("Close"), "blocks": [_context(text)]}


def _check_day(user: UserExtra, day: date) -> Optional[str]:
    """Why `user` can't write `day`, or None."""
    try:
        require_writer(user)
    except HTTPException as exc:
        return str(exc.detail)
    return FUTURE if day > daily_plans._today() else None


async def process_standup_click(slack_user_id: str, trigger_id: str, day: str, response_url: str) -> None:
    """Open the clicker's form for the nudge's day (weekend days too: extra work). Refusals are ephemeral. Never
    raises (a background task)."""
    try:
        user = await actions.slack_platform_user(slack_user_id)
        if user is None:
            await actions.reply(response_url, NOT_LINKED)
            return
        try:
            when = date.fromisoformat(day)
        except ValueError:
            log.warning("standup click from %s has a malformed day %r", slack_user_id, day)
            await actions.reply(response_url, actions.TRY_AGAIN)
            return
        refusal = _check_day(user, when)
        if refusal:
            await actions.reply(response_url, refusal)
            return
        # The trigger id lasts 3 s and building the day takes several queries: open a loading view now, fill it after.
        try:
            opened = await slack_bot._slack_api("views.open", {"trigger_id": trigger_id, "view": _note_view(LOADING)})
        except Exception as exc:  # noqa: BLE001
            log.warning("standup: views.open for %s failed: %s", slack_user_id, exc)
            await actions.reply(response_url, actions.FORM_FAILED)
            return
        view_id = opened["view"]["id"]
        try:
            view = await build_view(user, when)
        except Exception as exc:  # noqa: BLE001
            log.exception("standup form for %s on %s failed: %s", slack_user_id, when, exc)
            view = _note_view(FORM_ERROR)
        try:
            await slack_bot._slack_api("views.update", {"view_id": view_id, "view": view})
        except RuntimeError as exc:
            if str(exc) != "not_found":
                raise
            log.info("standup: %s closed the form before it loaded", slack_user_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("standup click from %s failed: %s", slack_user_id, exc)
        await actions.reply(response_url, actions.TRY_AGAIN)


async def process_standup_submission(view: dict, slack_user_id: str) -> None:
    """Save the submitted form through the app's day save (which posts or edits the standup post). Runs after the
    form closed, so refusals reach the submitter ephemerally in the daily plan channel. Never raises."""
    channel = (get_settings().SLACK_DAILY_PLAN_CHANNEL_ID or "").strip()

    async def tell(text: str) -> None:
        if channel:
            await actions.tell(channel, slack_user_id, text)
        else:
            log.warning("standup submission from %s: %s (no channel to say it in)", slack_user_id, text)

    try:
        user = await actions.slack_platform_user(slack_user_id)
        if user is None:
            await tell(NOT_LINKED)
            return
        try:
            when, body = _day(view), read_submission(view)
        except ValueError as exc:
            log.warning("standup submission from %s is malformed: %s", slack_user_id, exc)
            await tell(FORM_ERROR)
            return
        refusal = _check_day(user, when)
        if refusal:
            await tell(refusal)
            return
        await save_day(user, when, await _keep_unshown(user, when, body))
    except HTTPException as exc:
        await tell(str(exc.detail))
    except Exception as exc:  # noqa: BLE001
        log.exception("standup submission from %s failed: %s", slack_user_id, exc)
        await tell(FORM_ERROR)
