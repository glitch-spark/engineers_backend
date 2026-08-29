"""In-app alerts inbox + cron tick (no Slack)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.alerts_service import run_alerts_tick
from app.config import get_settings
from app.deps import get_current_user
from app.models import Notification, RecurringCharge, UserExtra
from app.routers._util import doc_to_dict, to_object_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["alerts"])
cron_router = APIRouter(prefix="/internal/alerts", tags=["alerts"])


def _serialize(doc: Notification) -> dict:
    payload = doc_to_dict(doc)
    payload["actions"] = _actions_for(doc)
    return payload


def _actions_for(doc: Notification) -> list[dict[str, str]]:
    if getattr(doc, "resolution", "none") in ("paid", "stopped"):
        return []
    if doc.kind == "card_renewal":
        return [
            {"key": "paid", "label": "Paid"},
            {"key": "go", "label": "Log"},
            {"key": "stop", "label": "Stop"},
        ]
    if doc.kind == "weekly_plan":
        return [{"key": "go", "label": "Go to weekly plan"}]
    if doc.kind == "daily_bids":
        return [{"key": "go", "label": "Go to resume"}]
    return [{"key": "go", "label": "Open"}]


@router.get("")
async def list_alerts(
    user: Annotated[UserExtra, Depends(get_current_user)],
    unread: Optional[bool] = Query(None),
    limit: int = Query(30, ge=1, le=100),
) -> dict:
    query: dict = {"userId": user.id}
    if unread is True:
        query["unread"] = True
    rows = await Notification.find(query).sort([("unread", -1), ("createdAt", -1)]).limit(limit).to_list()
    return {"alerts": [_serialize(r) for r in rows]}


@router.get("/unread-count")
async def unread_count(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    count = await Notification.find({"userId": user.id, "unread": True}).count()
    return {"count": count}


@router.post("/read-all")
async def read_all(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    result = await Notification.find({"userId": user.id, "unread": True}).update(
        {"$set": {"unread": False}}
    )
    modified = getattr(result, "modified_count", None)
    return {"ok": True, "updated": modified}


@router.post("/{alert_id}/read")
async def read_one(
    alert_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    doc = await _owned_alert(alert_id, user)
    doc.unread = False
    await doc.save()
    return _serialize(doc)


@router.post("/{alert_id}/actions/{action}")
async def run_action(
    alert_id: str,
    action: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    doc = await _owned_alert(alert_id, user)
    key = (action or "").strip().lower()
    if key == "go":
        await _resolve_related(doc, "opened")
        refreshed = await Notification.get(doc.id)
        return _serialize(refreshed or doc)
    if doc.kind != "card_renewal":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown action")

    charge = await _charge_for_alert(doc)
    if charge is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="charge not found")

    if key == "paid":
        charge.ackedPeriod = doc.periodKey
        await charge.save()
        await _resolve_related(doc, "paid")
    elif key == "stop":
        charge.status = "stopped"
        await charge.save()
        await _resolve_related(doc, "stopped")
    elif key == "stop_card":
        await RecurringCharge.find(
            {"userId": charge.userId, "cardLast4": charge.cardLast4, "status": "active"}
        ).update({"$set": {"status": "stopped"}})
        await _resolve_related(doc, "stopped")
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown action")

    refreshed = await Notification.get(doc.id)
    return _serialize(refreshed or doc)


async def _owned_alert(alert_id: str, user: UserExtra) -> Notification:
    oid = to_object_id(alert_id)
    doc = await Notification.get(oid)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if str(doc.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return doc


async def _charge_for_alert(doc: Notification) -> Optional[RecurringCharge]:
    raw = (doc.meta or {}).get("chargeId") or doc.dedupeId
    try:
        oid = PydanticObjectId(str(raw))
    except Exception:
        return None
    return await RecurringCharge.get(oid)


async def _resolve_related(doc: Notification, resolution: str) -> None:
    now = datetime.utcnow()
    await Notification.find(
        {
            "kind": doc.kind,
            "periodKey": doc.periodKey,
            "dedupeId": doc.dedupeId,
        }
    ).update({"$set": {
        "unread": False,
        "resolution": resolution,
        "resolvedAt": now,
    }})


@cron_router.post("/tick")
async def alerts_tick(
    x_cron_secret: Annotated[Optional[str], Header()] = None,
) -> dict:
    s = get_settings()
    expected = (s.SLACK_BOT_CRON_SECRET or "").strip()
    if not expected or (x_cron_secret or "").strip() != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    result = await run_alerts_tick()
    return {"ok": True, **result}
