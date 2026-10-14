import base64
import threading
import time
import uuid
from collections import deque

from fastapi import FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---- Your assigned values ----
TOTAL_ORDERS = 46          # T
RATE_LIMIT = 16            # R
WINDOW_SECONDS = 10

app = FastAPI()

# ---------- 3. Rate limiting (added FIRST so CORS wraps it) ----------
hits = {}                  # client_id -> deque of request timestamps
hits_lock = threading.Lock()


@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    if request.method == "OPTIONS":      # CORS preflight is free
        return await call_next(request)

    client_id = request.headers.get("x-client-id") or (
        request.client.host if request.client else "anonymous"
    )
    now = time.monotonic()

    with hits_lock:
        dq = hits.setdefault(client_id, deque())
        while dq and now - dq[0] >= WINDOW_SECONDS:   # forget old requests
            dq.popleft()
        if len(dq) >= RATE_LIMIT:
            retry_after = max(1, int(WINDOW_SECONDS - (now - dq[0])) + 1)
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": str(retry_after)},
            )
        dq.append(now)

    return await call_next(request)


# ---------- CORS (added LAST = outermost, so even 429s get CORS headers) ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)

# ---------- 1. Idempotent POST /orders ----------
orders_by_key = {}         # idempotency key -> order
orders_lock = threading.Lock()


@app.post("/orders", status_code=201)
async def create_order(
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        body = await request.json()
    except Exception:
        body = {}

    with orders_lock:
        if idempotency_key and idempotency_key in orders_by_key:
            return orders_by_key[idempotency_key]      # same order, no duplicate
        order = {"id": f"ord_{uuid.uuid4().hex[:12]}", "details": body}
        if idempotency_key:
            orders_by_key[idempotency_key] = order
        return order


# ---------- 2. Cursor pagination GET /orders ----------
def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
        return max(0, int(text.split(":")[1]))
    except Exception:
        return 0


@app.get("/orders")
def list_orders(limit: int = 10, cursor: str | None = None):
    limit = max(1, min(limit, 100))
    start = decode_cursor(cursor)                 # 0-based position
    end = min(start + limit, TOTAL_ORDERS)

    items = [{"id": i, "name": f"Order {i}"} for i in range(start + 1, end + 1)]
    next_cursor = encode_cursor(end) if end < TOTAL_ORDERS else None
    return {"items": items, "next_cursor": next_cursor}
