import os
import time
from typing import Optional

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


