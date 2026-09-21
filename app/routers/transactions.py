import re
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.alerts_service import upsert_charge_from_transaction
from app.deps import get_current_user
from app.models import Transaction, UserExtra
from app.routers._util import ci_regex, doc_to_dict, parse_date, populate_owner, to_object_id
from app.slack import notify_new_transaction, notify_transaction_status_change

router = APIRouter(prefix="/transactions", tags=["transactions"])


_LAST4_RE = re.compile(r"^\d{4}$")
_BILLING_CYCLES = ("one_time", "monthly")


def _validate_billing_cycle(value: Optional[str], *, required: bool = False) -> Optional[str]:
    if value is None or value == "":
        if required:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="billingCycle is required")
        return None
    if value not in _BILLING_CYCLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="billingCycle must be 'one_time' or 'monthly'",
        )
    return value


def _clean_card_label(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    return text or None


def _validate_pay_method(pay_method: Optional[str], card_last4: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Normalize + validate (payMethod, cardLast4). Accepts legacy 'crypto'
    spelling as an alias for 'coin' so older clients keep working. When
    method=='card', cardLast4 must be exactly 4 digits. When method=='coin',
    cardLast4 is forced to None. Returns the cleaned pair. Raises 400 on
    invalid input."""
    if pay_method is None:
        return None, None
    if pay_method == "crypto":
        pay_method = "coin"  # legacy alias
    if pay_method not in ("coin", "card"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="payMethod must be 'coin' or 'card'")
    if pay_method == "coin":
        return "coin", None
    last4 = (card_last4 or "").strip()
    if not _LAST4_RE.match(last4):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cardLast4 must be exactly 4 digits when payMethod='card'")
    return "card", last4


class TransactionCreate(BaseModel):
    date: str
    amount: float
    description: Optional[str] = ""
    notes: Optional[str] = ""
    payMethod: Optional[str] = None
    cardLast4: Optional[str] = None
    cardLabel: Optional[str] = None
    billingCycle: Optional[str] = "monthly"
    payerId: Optional[str] = None


class TransactionUpdate(BaseModel):
    date: Optional[str] = None
    amount: Optional[float] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    payMethod: Optional[str] = None
    cardLast4: Optional[str] = None
    cardLabel: Optional[str] = None
    billingCycle: Optional[str] = None
    payerId: Optional[str] = None


async def _resolve_payer_id(raw: Optional[str], default_id) -> Any:
    """Validate payerId against userextras. Empty/omitted → default (the owner)."""
    if raw is None or raw == "":
        return default_id
    oid = to_object_id(raw)
    found = await UserExtra.get(oid)
    if found is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="payer not found")
    return oid


def _format_totals(rows: list[dict], people: dict[str, UserExtra], id_key: str) -> list[dict]:
    out = []
    for r in rows:
        uid = str(r["_id"]) if r.get("_id") is not None else ""
        person = people.get(uid)
        out.append({
            id_key: uid,
            "name": (person.name if person else None) or (person.email if person else None) or uid,
            "email": person.email if person else None,
            "image": person.image if person else None,
            "income": float(r.get("income") or 0),
            "outcome": float(r.get("outcome") or 0),
            "net": float(r.get("net") or 0),
            "count": int(r.get("count") or 0),
        })
    return out


_TOTALS_GROUP = {
    "income": {"$sum": {"$cond": [{"$gt": ["$amount", 0]}, "$amount", 0]}},
    "outcome": {"$sum": {"$cond": [{"$lt": ["$amount", 0]}, {"$abs": "$amount"}, 0]}},
    "net": {"$sum": "$amount"},
    "count": {"$sum": 1},
}


@router.get("/card-hints")
async def card_hints(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Distinct last-4 + label pairs the current user has already logged."""
    pipeline = [
        {"$match": {
            "userId": user.id,
            "payMethod": "card",
            "cardLast4": {"$nin": [None, ""]},
        }},
        {"$sort": {"updatedAt": -1}},
        {"$group": {
            "_id": "$cardLast4",
            "cardLabel": {"$first": "$cardLabel"},
        }},
        {"$limit": 20},
    ]
    rows = await Transaction.aggregate(pipeline).to_list()
    return {
        "cards": [
            {"cardLast4": r["_id"], "cardLabel": r.get("cardLabel") or ""}
            for r in rows if r.get("_id")
        ]
    }


@router.get("/summary")
async def transactions_summary(
    user: Annotated[UserExtra, Depends(get_current_user)],
    userId: Optional[str] = Query(None),
    year: Optional[int] = Query(None),
) -> dict:
    is_admin = user.role == "admin"
    if year is None:
        year = datetime.utcnow().year

    match: dict[str, Any] = {}
    if is_admin:
        if userId:
            match["userId"] = to_object_id(userId)
    else:
        match["userId"] = user.id

    start = datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(year, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc)
    match["date"] = {"$gte": start, "$lte": end}

    monthly_pipeline = [
        {"$match": match},
        {"$group": {"_id": {"$dateToString": {"date": "$date", "format": "%Y-%m"}}, "total": {"$sum": "$amount"}}},
        {"$sort": {"_id": 1}},
    ]
    monthly_rows = await Transaction.aggregate(monthly_pipeline).to_list()

    stats_pipeline = [
        {"$match": match},
        {"$group": {
            "_id": None,
            "totalAmount": {"$sum": "$amount"},
            "totalCount": {"$sum": 1},
            "avgAmount": {"$avg": "$amount"},
            "minAmount": {"$min": "$amount"},
            "maxAmount": {"$max": "$amount"},
        }},
    ]
    stats_result = await Transaction.aggregate(stats_pipeline).to_list()
    stats = stats_result[0] if stats_result else {
        "totalAmount": 0, "totalCount": 0, "avgAmount": 0, "minAmount": 0, "maxAmount": 0,
    }

    status_pipeline = [
        {"$match": match},
        {"$group": {"_id": "$status", "count": {"$sum": 1}, "total": {"$sum": "$amount"}}},
        {"$sort": {"_id": 1}},
    ]
    status_rows = await Transaction.aggregate(status_pipeline).to_list()
    status_breakdown = {row["_id"]: {"count": row["count"], "total": row["total"]} for row in status_rows}

    return {
        "monthly": [{"period": r["_id"], "total": r["total"], "year": year} for r in monthly_rows],
        "stats": {
            "totalAmount": stats.get("totalAmount", 0),
            "totalCount": stats.get("totalCount", 0),
            "avgAmount": stats.get("avgAmount", 0),
            "minAmount": stats.get("minAmount", 0),
            "maxAmount": stats.get("maxAmount", 0),
        },
        "statusBreakdown": status_breakdown,
    }


@router.get("")
async def list_transactions(
    user: Annotated[UserExtra, Depends(get_current_user)],
    page: int = Query(1),
    limit: int = Query(10),
    from_: str = Query("", alias="from"),
    to: str = Query(""),
    userId: str = Query(""),
    search: str = Query(""),
    fromSearch: str = Query(""),
    toSearch: str = Query(""),
    userSearch: str = Query(""),
    payMethod: str = Query(""),
) -> dict:
    is_admin = user.role == "admin"
    query: dict[str, Any] = {} if is_admin else {"userId": user.id}

    date_filter: dict = {}
    f_dt = parse_date(from_)
    t_dt = parse_date(to, end_of_day=True)
    if f_dt:
        date_filter["$gte"] = f_dt
    if t_dt:
        date_filter["$lte"] = t_dt
    if date_filter:
        query["date"] = date_filter

    if is_admin and userId:
        query["userId"] = to_object_id(userId)

    if payMethod == "crypto":
        payMethod = "coin"  # legacy alias from older clients
    if payMethod in ("coin", "card"):
        query["payMethod"] = payMethod

    if search:
        query["$or"] = [
            {"description": ci_regex(search)},
            {"notes": ci_regex(search)},
            {"userId.name": ci_regex(search)},
            {"userId.email": ci_regex(search)},
        ]

    extra: list[dict] = []
    if fromSearch:
        extra.append({"$expr": {"$regexMatch": {
            "input": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "regex": re.escape(fromSearch),
            "options": "i",
        }}})
    if toSearch:
        extra.append({"$expr": {"$regexMatch": {
            "input": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
            "regex": re.escape(toSearch),
            "options": "i",
        }}})
    if userSearch:
        extra.append({"$or": [
            {"userId.name": ci_regex(userSearch)},
            {"userId.email": ci_regex(userSearch)},
        ]})
    if extra:
        query["$and"] = extra

    skip = (page - 1) * limit
    total = await Transaction.find(query).count()
    rows = await Transaction.find(query).sort([("date", -1)]).skip(skip).limit(limit).to_list()
    txs_out = await populate_owner(rows, field="userId", extra=(("payerId", "payer"),))
    for item in txs_out:
        # Pre-payer rows have no payerId — surface the owner so the table
        # still has a Payer cell.
        if not item.get("payerName") and not item.get("payerEmail"):
            item["payerName"] = item.get("ownerName")
            item["payerEmail"] = item.get("ownerEmail")
            item["payerImage"] = item.get("ownerImage")
            if item.get("payerId") is None:
                item["payerId"] = item.get("userId")

    owner_pipeline = [
        {"$match": query},
        {"$group": {"_id": "$userId", **_TOTALS_GROUP}},
        {"$sort": {"net": -1}},
    ]
    payer_pipeline = [
        {"$match": query},
        {"$group": {"_id": {"$ifNull": ["$payerId", "$userId"]}, **_TOTALS_GROUP}},
        {"$sort": {"outcome": -1}},
    ]
    owner_rows = await Transaction.aggregate(owner_pipeline).to_list()
    payer_rows = await Transaction.aggregate(payer_pipeline).to_list()
    people_ids = [
        r["_id"] for r in owner_rows + payer_rows if r.get("_id") is not None
    ]
    people: dict[str, UserExtra] = {}
    if people_ids:
        for u in await UserExtra.find({"_id": {"$in": people_ids}}).to_list():
            people[str(u.id)] = u

    user_totals = _format_totals(owner_rows, people, "userId")
    payer_totals = _format_totals(payer_rows, people, "payerId")
    total_outcome = sum(p["outcome"] for p in payer_totals)

    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "transactions": txs_out,
        "userTotals": user_totals,
        "payerTotals": payer_totals,
        "totalOutcome": total_outcome,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "totalPages": total_pages,
            "hasNext": page < total_pages,
            "hasPrev": page > 1,
        },
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_transaction(
    body: TransactionCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if not body.date or body.amount is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date and amount are required")

    date_dt = parse_date(body.date)
    if date_dt is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid date")

    if not body.payMethod:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="payMethod is required")
    pay_method, card_last4 = _validate_pay_method(body.payMethod, body.cardLast4)
    billing_cycle = _validate_billing_cycle(body.billingCycle) or "monthly"
    card_label = _clean_card_label(body.cardLabel) if pay_method == "card" else None
    payer_id = await _resolve_payer_id(body.payerId, user.id)

    tx = Transaction(
        userId=user.id,
        date=date_dt,
        amount=body.amount,
        description=body.description or "",
        notes=body.notes or "",
        status="pending",
        payMethod=pay_method,
        cardLast4=card_last4,
        cardLabel=card_label,
        billingCycle=billing_cycle,
        payerId=payer_id,
    )
    await tx.insert()
    await upsert_charge_from_transaction(tx)

    await notify_new_transaction(
        creator_email=user.email or "",
        amount=float(body.amount),
        date=date_dt.strftime("%Y-%m-%d"),
        description=body.description or "",
    )

    return doc_to_dict(tx)


@router.put("/{tx_id}")
async def update_transaction(
    tx_id: str,
    body: TransactionUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(tx_id)
    tx = await Transaction.get(oid)
    if tx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    is_admin = user.role == "admin"
    is_owner = str(tx.userId) == str(user.id)
    if not is_admin and not is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    if is_admin and body.status and body.status in ("approved", "rejected"):
        if tx.status != body.status:
            tx.status = body.status
            tx.approvedBy = user.id
            tx.approvedAt = datetime.utcnow()
            owner = await UserExtra.get(tx.userId)
            await notify_transaction_status_change(
                tx_id=str(tx.id),
                new_status=body.status,
                approver_email=user.email or "",
                owner_email=(owner.email if owner else None),
                amount=tx.amount,
                date=tx.date.strftime("%Y-%m-%d") if tx.date else None,
                notes=tx.notes,
            )

    if tx.status == "pending" or is_admin:
        if body.date is not None:
            d = parse_date(body.date)
            if d is not None:
                tx.date = d
        if body.amount is not None:
            tx.amount = body.amount
        if body.description is not None:
            tx.description = body.description
        if body.notes is not None:
            tx.notes = body.notes
        if body.payMethod is not None:
            # Allow caller to send only payMethod; validate uses the
            # incoming cardLast4 when present, else the row's current one.
            pm, l4 = _validate_pay_method(
                body.payMethod,
                body.cardLast4 if body.cardLast4 is not None else tx.cardLast4,
            )
            tx.payMethod = pm
            tx.cardLast4 = l4
        elif body.cardLast4 is not None and tx.payMethod == "card":
            # Updating only last4 on an existing card payment.
            pm, l4 = _validate_pay_method("card", body.cardLast4)
            tx.cardLast4 = l4
        if body.cardLabel is not None:
            tx.cardLabel = _clean_card_label(body.cardLabel) if tx.payMethod == "card" else None
        if body.billingCycle is not None:
            tx.billingCycle = _validate_billing_cycle(body.billingCycle) or tx.billingCycle
        if body.payerId is not None:
            tx.payerId = await _resolve_payer_id(body.payerId, tx.userId)

    await tx.save()
    await upsert_charge_from_transaction(tx)
    return doc_to_dict(tx)


@router.delete("/{tx_id}")
async def delete_transaction(
    tx_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(tx_id)
    tx = await Transaction.get(oid)
    if tx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    is_admin = user.role == "admin"
    is_owner = str(tx.userId) == str(user.id)
    if not is_admin and not is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    await tx.delete()
    return {"ok": True}
