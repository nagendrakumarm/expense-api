import os
from fastapi.middleware.cors import CORSMiddleware
import time
from typing import Optional
from datetime import date
from fastapi import Query

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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


class LoginRequest(BaseModel):
    username: str
    password: str


class TransactionCreate(BaseModel):
    date: str
    store: str
    description: Optional[str] = None
    amount: float
    category: int


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
            "categoryType": type_map.get(int(row["type"]), "Expense")
            if row.get("type") is not None
            else "Expense",
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
    rows = fetch_enriched_transactions()
    return summarize_month(rows, month.strip())


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
            "categoryType": type_map.get(int(row["type"]), "Expense")
            if row.get("type") is not None
            else "Expense",
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


class TransactionUpdate(BaseModel):
    date: Optional[str] = None
    store: Optional[str] = None
    description: Optional[str] = None
    amount: Optional[float] = None
    category: Optional[int] = None


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

def _trailing_year_months(anchor_month: str, count: int) -> list[str]:
    parts = anchor_month.strip().split("-")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="anchor_month must be YYYY-MM")
    try:
        yr, mo = int(parts[0]), int(parts[1])
        if not (1 <= mo <= 12):
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid anchor_month")

    result: list[str] = []
    for offset in range(count - 1, -1, -1):
        month_index = (yr * 12 + (mo - 1)) - offset
        target_yr, target_mo0 = divmod(month_index, 12)
        result.append(f"{target_yr:04d}-{target_mo0 + 1:02d}")
    return result


@app.get("/api/comparison")
def get_category_comparison(
    anchor_month: str = Query(default="2026-11"),
    months: int = Query(default=3, ge=1, le=24),
    _user: str = Depends(verify_bearer_token),
):
    ym_list = _trailing_year_months(anchor_month, months)
    month_set = set(ym_list)
    start_date = f"{ym_list[0]}-01"

    first_yr, first_mo = map(int, ym_list[-1].split("-"))
    if first_mo == 12:
        end_date = f"{first_yr + 1:04d}-01-01"
    else:
        end_date = f"{first_yr:04d}-{first_mo + 1:02d}-01"

    cats_res = supabase.table("Categories").select("*").execute()
    categories_rows = cats_res.data or []

    cat_subtype_by_id: dict[int, str] = {}
    cat_type_by_id: dict[int, str] = {}
    for c in categories_rows:
        cid = int(c["id"])
        cat_subtype_by_id[cid] = str(c.get("subtype") or "Uncategorized").strip()
        cat_type_by_id[cid] = str(c.get("categoryType") or c.get("type") or "Expense").strip()

    tx_res = (
        supabase.table("Transactions")
        .select("*")
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
        cat_type = str(row.get("categoryType") or cat_type_by_id.get(cid) or "Expense").lower()
        if "income" in cat_type:
            continue

        subtype = str(row.get("subtype") or cat_subtype_by_id.get(cid) or "Uncategorized").strip()
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