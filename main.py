import os
import re
import time
from datetime import date
from typing import Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import Client, create_client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
APP_LOGIN_USER = os.getenv("APP_LOGIN_USER")
APP_LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD")
TOKEN_SECRET = os.getenv("TOKEN_SECRET")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not TOKEN_SECRET:
    raise RuntimeError("Missing SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, or TOKEN_SECRET")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
app = FastAPI(title="Expense Tracker API")

raw_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "https://nagendrakumarm.github.io,http://localhost:5500",
)
allowed_origins = [
    origin.strip().rstrip("/")
    for origin in raw_origins.split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def current_year_month() -> str:
    return date.today().strftime("%Y-%m")


def validate_year_month(ym: str) -> str:
    cleaned = ym.strip()
    if not YEAR_MONTH_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="Month must be in YYYY-MM format")
    return cleaned


# --- Models ---


class LoginRequest(BaseModel):
    username: str
    password: str


class TransactionCreate(BaseModel):
    date: str
    store: str
    description: Optional[str] = None
    amount: float
    category: int


class TransactionUpdate(BaseModel):
    date: Optional[str] = None
    store: Optional[str] = None
    description: Optional[str] = None
    amount: Optional[float] = None
    category: Optional[int] = None


class PaymentCreateItem(BaseModel):
    account_id: int
    amount: float = Field(..., ge=0)
    month: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    is_paid: bool = False


class PaymentBatchCreate(BaseModel):
    payments: list[PaymentCreateItem] = Field(..., min_length=1)


class PaymentPatch(BaseModel):
    is_paid: Optional[bool] = None
    paid: Optional[bool] = None
    amount: Optional[float] = Field(default=None, ge=0)
    month: Optional[str] = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    account_id: Optional[int] = None


# --- Auth ---


def create_access_token(username: str, expires_in_days: int = 30) -> str:
    payload = {
        "sub": username,
        "exp": int(time.time()) + expires_in_days * 86400,
    }
    return jwt.encode(payload, TOKEN_SECRET, algorithm="HS256")


def verify_bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = jwt.decode(token, TOKEN_SECRET, algorithms=["HS256"])
        return str(decoded["sub"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session token expired; please sign in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session token")


@app.post("/api/login")
def login(creds: LoginRequest):
    if not APP_LOGIN_USER or not APP_LOGIN_PASSWORD:
        raise HTTPException(status_code=500, detail="Login credentials not configured on server")
    if creds.username != APP_LOGIN_USER or creds.password != APP_LOGIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token(creds.username)
    return {"access_token": token, "token_type": "bearer"}


# --- Categories & Summary ---


@app.get("/api/categories")
def get_categories(_user: str = Depends(verify_bearer_token)):
    types_res = supabase.table("CategoryType").select("id, type").execute()
    cats_res = (
        supabase.table("Categories")
        .select("id, type, subtype")
        .order("subtype", desc=False)
        .execute()
    )
    type_map = {int(row["id"]): row.get("type") or "Expense" for row in (types_res.data or [])}
    categories = [
        {
            "id": row["id"],
            "typeId": row["type"],
            "categoryType": (
                type_map.get(int(row["type"]), "Expense")
                if row.get("type") is not None
                else "Expense"
            ),
            "subtype": (row.get("subtype") or "").strip(),
        }
        for row in (cats_res.data or [])
    ]
    return {"categories": categories}


@app.get("/api/summary")
def get_summary(
    month: str = Query(default=""),
    _user: str = Depends(verify_bearer_token),
):
    target_month = validate_year_month(month) if month.strip() else current_year_month()
    rows = fetch_enriched_transactions()
    return summarize_month(rows, target_month)


def fetch_enriched_transactions() -> list[dict]:
    types_res = supabase.table("CategoryType").select("id, type").execute()
    cats_res = supabase.table("Categories").select("id, type, subtype").execute()
    tx_res = (
        supabase.table("Transactions")
        .select("id, date, store, description, amount, category")
        .order("date", desc=True)
        .execute()
    )

    type_map = {int(row["id"]): row.get("type") or "Expense" for row in (types_res.data or [])}
    cat_map = {
        int(row["id"]): {
            "subtype": (row.get("subtype") or "Uncategorized").strip(),
            "categoryType": (
                type_map.get(int(row["type"]), "Expense")
                if row.get("type") is not None
                else "Expense"
            ),
        }
        for row in (cats_res.data or [])
    }

    enriched: list[dict] = []
    for row in tx_res.data or []:
        cat_id = int(row["category"]) if row.get("category") is not None else -1
        cat = cat_map.get(cat_id, {"subtype": "Uncategorized", "categoryType": "Expense"})
        raw_desc = row.get("description")
        enriched.append(
            {
                **row,
                "store": str(row.get("store") or "").strip(),
                "description": str(raw_desc).strip() if raw_desc is not None else "",
                "subtype": cat["subtype"],
                "categoryType": cat["categoryType"],
            }
        )
    return enriched


def summarize_month(rows: list[dict], year_month: str = "") -> dict:
    month_rows = (
        [r for r in rows if str(r.get("date") or "").startswith(year_month)]
        if year_month
        else rows
    )

    income_by_category: dict[str, float] = {}
    expense_by_category: dict[str, float] = {}
    total_income = 0.0
    total_expenses = 0.0

    for row in month_rows:
        amt = abs(float(row.get("amount") or 0.0))
        label = str(row.get("subtype") or "Uncategorized").strip()
        is_income = "income" in str(row.get("categoryType") or "").lower()

        if is_income:
            total_income += amt
            income_by_category[label] = income_by_category.get(label, 0.0) + amt
        else:
            total_expenses += amt
            expense_by_category[label] = expense_by_category.get(label, 0.0) + amt

    return {
        "month": year_month or "all",
        "totalIncome": total_income,
        "totalExpenses": total_expenses,
        "netBalance": total_income - total_expenses,
        "incomeByCategory": income_by_category,
        "expenseByCategory": expense_by_category,
        "transactions": month_rows,
    }


# --- Transactions ---


@app.post("/api/transactions", status_code=201)
def create_transaction(
    payload: TransactionCreate,
    _user: str = Depends(verify_bearer_token),
):
    clean_store = payload.store.strip()
    clean_description = payload.description.strip() if payload.description else None
    if not payload.date.strip() or not clean_store:
        raise HTTPException(status_code=400, detail="date and store are required")

    insert_res = (
        supabase.table("Transactions")
        .insert(
            {
                "date": payload.date.strip(),
                "store": clean_store,
                "description": clean_description,
                "amount": float(payload.amount),
                "category": int(payload.category),
            }
        )
        .execute()
    )
    created = (insert_res.data or [None])[0]
    if not created:
        raise HTTPException(status_code=500, detail="Insert succeeded without returned row")
    return {"transaction": created}


@app.patch("/api/transactions/{tx_id}")
def patch_transaction(
    tx_id: int,
    payload: TransactionUpdate,
    _user: str = Depends(verify_bearer_token),
):
    updates: dict = {}

    if payload.date is not None:
        clean_date = payload.date.strip()
        if not clean_date:
            raise HTTPException(status_code=400, detail="date cannot be empty")
        updates["date"] = clean_date

    if payload.store is not None:
        clean_store = payload.store.strip()
        if not clean_store:
            raise HTTPException(status_code=400, detail="store cannot be empty")
        updates["store"] = clean_store

    if payload.description is not None:
        clean_desc = payload.description.strip()
        updates["description"] = clean_desc if clean_desc else None

    if payload.amount is not None:
        updates["amount"] = abs(float(payload.amount))

    if payload.category is not None:
        updates["category"] = int(payload.category)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    res = (
        supabase.table("Transactions")
        .update(updates)
        .eq("id", tx_id)
        .execute()
    )
    updated = (res.data or [None])[0]
    if not updated:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"transaction": updated}


@app.delete("/api/transactions/{tx_id}")
def delete_transaction(
    tx_id: int,
    _user: str = Depends(verify_bearer_token),
):
    res = (
        supabase.table("Transactions")
        .delete()
        .eq("id", tx_id)
        .execute()
    )
    deleted = (res.data or [None])[0]
    if not deleted:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return {"deleted": True, "id": tx_id}


# --- Multi-Month Category Comparison ---


def _trailing_year_months(anchor_month: str, count: int) -> list[str]:
    cleaned = validate_year_month(anchor_month)
    yr, mo = map(int, cleaned.split("-"))

    result: list[str] = []
    for offset in range(count - 1, -1, -1):
        month_index = (yr * 12 + (mo - 1)) - offset
        target_yr, target_mo0 = divmod(month_index, 12)
        result.append(f"{target_yr:04d}-{target_mo0 + 1:02d}")
    return result


@app.get("/api/comparison")
def get_category_comparison(
    anchor_month: str = Query(default=""),
    months: int = Query(default=3, ge=1, le=24),
    _user: str = Depends(verify_bearer_token),
):
    anchor = anchor_month.strip() or current_year_month()
    ym_list = _trailing_year_months(anchor, months)
    month_set = set(ym_list)
    start_date = f"{ym_list[0]}-01"

    last_yr, last_mo = map(int, ym_list[-1].split("-"))
    if last_mo == 12:
        end_date = f"{last_yr + 1:04d}-01-01"
    else:
        end_date = f"{last_yr:04d}-{last_mo + 1:02d}-01"

    types_res = supabase.table("CategoryType").select("id, type").execute()
    type_name_by_id: dict[int, str] = {
        int(t["id"]): str(t.get("type") or "Expense").strip()
        for t in (types_res.data or [])
    }

    cats_res = supabase.table("Categories").select("id, type, subtype").execute()
    categories_rows = cats_res.data or []

    cat_subtype_by_id: dict[int, str] = {}
    cat_type_by_id: dict[int, str] = {}
    for c in categories_rows:
        cid = int(c["id"])
        type_id = int(c["type"]) if c.get("type") is not None else -1
        cat_subtype_by_id[cid] = str(c.get("subtype") or "Uncategorized").strip()
        cat_type_by_id[cid] = type_name_by_id.get(type_id, "Expense")

    tx_res = (
        supabase.table("Transactions")
        .select("id, date, amount, category")
        .gte("date", start_date)
        .lt("date", end_date)
        .execute()
    )
    tx_rows = tx_res.data or []

    totals_by_month: dict[str, dict[str, float]] = {ym: {} for ym in ym_list}
    expense_categories: set[str] = set()

    for row in tx_rows:
        ym = str(row.get("date") or "")[:7]
        if ym not in month_set:
            continue

        cid = int(row.get("category") or 0)
        cat_type = cat_type_by_id.get(cid, "Expense").lower()
        if "income" in cat_type:
            continue

        subtype = cat_subtype_by_id.get(cid, "Uncategorized")
        amount = abs(float(row.get("amount") or 0.0))

        expense_categories.add(subtype)
        totals_by_month[ym][subtype] = round(totals_by_month[ym].get(subtype, 0.0) + amount, 2)

    sorted_categories = sorted(expense_categories, key=str.lower)
    for ym in ym_list:
        for cat in sorted_categories:
            totals_by_month[ym].setdefault(cat, 0.0)

    return {
        "months": ym_list,
        "categories": sorted_categories,
        "totals_by_month": totals_by_month,
    }

# --- Accounts & Monthly Payments ---


def _fetch_accounts() -> tuple[list[dict], dict[int, dict]]:
    res = (
        supabase.table("Accounts")
        .select("id, name, type, duedate, isactive")
        .order("id", desc=False)
        .execute()
    )
    rows = res.data or []
    accounts: list[dict] = []
    by_id: dict[int, dict] = {}

    for row in rows:
        is_active = row.get("isactive")
        if is_active is False:
            continue
        acct = {
            "id": int(row["id"]),
            "name": str(row.get("name") or "").strip(),
            "type": int(row["type"]) if row.get("type") is not None else None,
            "duedate": int(row["duedate"]) if row.get("duedate") is not None else 15,
            "isactive": True if is_active is None else bool(is_active),
        }
        accounts.append(acct)
        by_id[acct["id"]] = acct

    return accounts, by_id


def _serialize_payment_row(row: dict, account_by_id: dict[int, dict]) -> dict:
    acct_id = int(row["account_id"]) if row.get("account_id") is not None else None
    acct = account_by_id.get(acct_id) if acct_id is not None else None
    account_name = acct["name"] if acct else (f"Account #{acct_id}" if acct_id is not None else "Unassigned Account")
    due_day = acct["duedate"] if acct else 15
    clean_amount = float(row.get("amount") or 0.0)
    is_paid = True if clean_amount <= 0 else bool(row.get("is_paid"))
    
    return {
        "id": row["id"],
        "account_id": acct_id,
        "account_name": account_name,
        "title": account_name,
        "amount": clean_amount,
        "due_day": due_day,
        "month": str(row.get("month") or "").strip(),
        "year_month": str(row.get("month") or "").strip(),
        "is_paid": is_paid,
        "paid": is_paid,
    }


@app.get("/api/payments")
def get_payments(
    month: str = Query(default=""),
    scope: str = Query(default="all"),
    _user: str = Depends(verify_bearer_token),
):
    accounts, account_by_id = _fetch_accounts()
    query = supabase.table("monthly_payments").select("*")
    if scope == "month" and month.strip():
        target_month = validate_year_month(month)
        query = query.eq("month", target_month)
    res = query.order("month", desc=False).order("id", desc=False).execute()
    payments = [_serialize_payment_row(r, account_by_id) for r in (res.data or [])]
    return {"accounts": accounts, "payments": payments}


@app.post("/api/payments", status_code=201)
def create_payments(
    payload: PaymentBatchCreate,
    _user: str = Depends(verify_bearer_token),
):
    _, account_by_id = _fetch_accounts()
    records_to_insert: list[dict] = []

    for item in payload.payments:
        acct_id = int(item.account_id)
        if acct_id not in account_by_id:
            raise HTTPException(status_code=404, detail=f"Account {acct_id} not found")

        clean_amount = round(abs(float(item.amount)), 2)
        records_to_insert.append(
            {
                "account_id": acct_id,
                "amount": clean_amount,
                "month": validate_year_month(item.month),
                "is_paid": True if clean_amount ==0 else bool(item.is_paid),
            }
        )

    res = (
        supabase.table("monthly_payments")
        .upsert(records_to_insert, on_conflict="account_id,month")
        .execute()
    )
    inserted_rows = res.data or []
    if not inserted_rows:
        raise HTTPException(status_code=500, detail="Upsert into monthly_payments returned no rows")

    return [_serialize_payment_row(r, account_by_id) for r in inserted_rows]


@app.patch("/api/payments/{payment_id}")
def patch_payment(
    payment_id: int,
    payload: PaymentPatch,
    _user: str = Depends(verify_bearer_token),
):
    _, account_by_id = _fetch_accounts()
    updates: dict = {}

    paid_val = payload.is_paid if payload.is_paid is not None else payload.paid
    if paid_val is not None:
        updates["is_paid"] = bool(paid_val)
    if payload.amount is not None:
        clean_amount = round(abs(float(payload.amount)), 2)
        updates["amount"] = clean_amount
        if clean_amount == 0:
            updates["is_paid"] = True
    if payload.month is not None:
        updates["month"] = validate_year_month(payload.month)
    if payload.account_id is not None:
        acct_id = int(payload.account_id)
        if acct_id not in account_by_id:
            raise HTTPException(status_code=404, detail=f"Account {acct_id} not found")
        updates["account_id"] = acct_id

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    res = (
        supabase.table("monthly_payments")
        .update(updates)
        .eq("id", payment_id)
        .execute()
    )
    updated = (res.data or [None])[0]
    if not updated:
        raise HTTPException(status_code=404, detail="Payment not found")

    return _serialize_payment_row(updated, account_by_id)


@app.delete("/api/payments/{payment_id}")
def delete_payment(
    payment_id: int,
    _user: str = Depends(verify_bearer_token),
):
    res = (
        supabase.table("monthly_payments")
        .delete()
        .eq("id", payment_id)
        .execute()
    )
    deleted = (res.data or [None])[0]
    if not deleted:
        raise HTTPException(status_code=404, detail="Payment not found")
    return {"deleted": True, "id": payment_id}