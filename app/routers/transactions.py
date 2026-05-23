import re
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import Transaction, UserExtra
from app.routers._util import ci_regex, doc_to_dict, parse_date, populate_owner, to_object_id
from app.slack import notify_new_transaction, notify_transaction_status_change

router = APIRouter(prefix="/transactions", tags=["transactions"])


_LAST4_RE = re.compile(r"^\d{4}$")


def _validate_pay_method(pay_method: Optional[str], card_last4: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Normalize + validate (payMethod, cardLast4). When method=='card',
    cardLast4 must be exactly 4 digits. When method=='crypto', cardLast4
    is forced to None. Returns the cleaned pair. Raises 400 on invalid."""
    if pay_method is None:
        return None, None
    if pay_method not in ("crypto", "card"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="payMethod must be 'crypto' or 'card'")
    if pay_method == "crypto":
        return "crypto", None
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


class TransactionUpdate(BaseModel):
    date: Optional[str] = None
    amount: Optional[float] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None
    payMethod: Optional[str] = None
    cardLast4: Optional[str] = None


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

    if payMethod in ("crypto", "card"):
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
    txs_out = await populate_owner(rows, field="userId")

    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "transactions": txs_out,
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

    tx = Transaction(
        userId=user.id,
        date=date_dt,
        amount=body.amount,
        description=body.description or "",
        notes=body.notes or "",
        status="pending",
        payMethod=pay_method,
        cardLast4=card_last4,
    )
    await tx.insert()

    try:
        await notify_new_transaction(
            creator_email=user.email or "",
            amount=float(body.amount),
            date=date_dt.strftime("%Y-%m-%d"),
            description=body.description or "",
        )
    except Exception:
        pass

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
            try:
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
            except Exception:
                pass

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

    await tx.save()
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
